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

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from ._auth.requirements import AuthRequirement, SetRequirement, merge_requirements
from ._http import _is_http_token
from ._route_lifecycle import lifecycle_headers
from ._routing import Handler, check_placeholders
from ._structured_fields import Item, Token, serialize_list
from .binding import Depends
from .middleware.base import Middleware, MiddlewareHooks

_EMPTY_REQUIREMENT = AuthRequirement()


def _query_media_range(value: str) -> tuple[str, Item]:
    if not isinstance(value, str):
        raise TypeError(f"Accept-Query media ranges must be str, not {type(value).__name__}")
    media_type, separator, parameters = value.partition(";")
    media_type = media_type.strip().lower()
    major, slash, minor = media_type.partition("/")
    if (
        not slash
        or not major
        or not minor
        or "/" in minor
        or not _is_http_token(major)
        or not _is_http_token(minor)
        or major == "*"
        and minor != "*"
    ):
        raise ValueError(
            f"invalid Accept-Query media range {value!r}; use type/subtype, type/*, or */*"
        )
    structured_parameters: dict[str, str | Token] = {}
    if separator:
        for raw_parameter in parameters.split(";"):
            name, equals, raw_value = raw_parameter.strip().partition("=")
            if not equals or not name or not raw_value:
                raise ValueError(
                    f"invalid Accept-Query media range {value!r}; parameters must be name=value"
                )
            if not _is_http_token(name):
                raise ValueError(f"invalid Accept-Query parameter name {name!r}; use an HTTP token")
            if raw_value.startswith('"'):
                if len(raw_value) < 2 or not raw_value.endswith('"'):
                    raise ValueError(
                        f"invalid Accept-Query parameter {raw_parameter!r}; close the quoted value"
                    )
                parameter_value: str | Token = (
                    raw_value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                )
            elif _is_http_token(raw_value):
                try:
                    parameter_value = Token(raw_value)
                except ValueError:
                    parameter_value = raw_value
            else:
                raise ValueError(
                    f"invalid Accept-Query parameter value {raw_value!r}; "
                    "use an HTTP token or quoted string"
                )
            structured_parameters[name.lower()] = parameter_value
    item_value: str | Token
    if structured_parameters:
        try:
            item_value = Token(media_type)
        except ValueError:
            item_value = media_type
    else:
        item_value = media_type
    return media_type, Item(item_value, structured_parameters)


class _QueryContentPolicy:
    __slots__ = ("_accept_query", "_media_types")

    def __init__(self, media_ranges: Iterable[str]) -> None:
        parsed = tuple(_query_media_range(value) for value in media_ranges)
        if not parsed:
            raise ValueError(
                "accept_query must name at least one media range; use ('*/*',) for any"
            )
        self._media_types = tuple(media_type for media_type, _item in parsed)
        self._accept_query = serialize_list(item for _media_type, item in parsed)

    def describe(self) -> Any:
        from .middleware.base import HeaderSpec, MiddlewareContract
        from .openapi import ResponseSpec

        header = HeaderSpec(
            "Accept-Query",
            description="Structured list of request media types this QUERY resource accepts.",
            const=self._accept_query.decode("ascii"),
        )
        return MiddlewareContract(
            response_headers=((None, header), (400, header), (415, header)),
            responses=(
                (
                    400,
                    ResponseSpec(
                        description="The QUERY Content-Type is missing, duplicated, or malformed.",
                        media_type="application/problem+json",
                    ),
                ),
                (
                    415,
                    ResponseSpec(
                        description="The QUERY Content-Type is not supported by this resource.",
                        media_type="application/problem+json",
                    ),
                ),
            ),
            methods=frozenset({"QUERY"}),
        )

    def before_sync(self, request: Any) -> Any:
        from .response import ProblemResponse

        values = [value for name, value in request.headers if name == b"content-type"]
        if len(values) != 1:
            return ProblemResponse(
                status=400,
                detail="QUERY requires exactly one Content-Type request header",
            )
        try:
            received = values[0].decode("ascii").partition(";")[0].strip().lower()
        except UnicodeDecodeError:
            return ProblemResponse(
                status=400,
                detail="QUERY Content-Type must be ASCII",
            )
        if not any(
            offered == "*/*"
            or offered.endswith("/*")
            and received.startswith(offered[:-1])
            or offered == received
            for offered in self._media_types
        ):
            return ProblemResponse(
                status=415,
                detail=f"Content-Type {received!r} is not supported for this QUERY resource",
            )
        return None

    def after_sync(self, request: Any, response: Any) -> Any:
        from ._prepared import PreparedResponse
        from ._webpolicy import replace_response_header

        if isinstance(response, PreparedResponse):
            headers = list(response.headers)
            replace_response_header(headers, b"accept-query", self._accept_query)
            return PreparedResponse(
                response.body,
                status=response.status,
                media_type=None,
                headers=headers,
            )
        replace_response_header(response.headers, b"accept-query", self._accept_query)
        return response

    def middleware(self) -> MiddlewareHooks:
        return MiddlewareHooks(
            before_sync=self.before_sync,
            after_sync=self.after_sync,
            contract=self.describe(),
        )


def _prefix(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("/"):
        raise ValueError("router prefixes must begin with '/'")
    if value != "/" and value.endswith("/"):
        value = value.rstrip("/")
    return value


def _path(prefix: str, path: str) -> str:
    _validate_route_path(path)
    if not prefix:
        return path
    if path == "/":
        return prefix or "/"
    return prefix + path


def _validate_route_path(path: str) -> None:
    if not path.startswith("/"):
        raise ValueError("route paths must begin with '/'")
    check_placeholders(path)


def _validate_status_code(status_code: int) -> None:
    if not 100 <= status_code <= 599:
        raise ValueError("status_code must be between 100 and 599")


def _permission_requirement(permissions: Iterable[str]) -> AuthRequirement:
    values = frozenset(permissions)
    if not values:
        return AuthRequirement()
    return AuthRequirement(
        authenticated=True,
        permission_checks=(SetRequirement(values, "all"),),
    )


def _route_definition(
    path: str,
    endpoint: Handler,
    *,
    methods: Iterable[str],
    middleware: Iterable[Middleware] = (),
    tags: Iterable[str] = (),
    summary: str | None = None,
    dependencies: Iterable[Depends] = (),
    permissions: Iterable[str] = (),
    requirement: AuthRequirement = _EMPTY_REQUIREMENT,
    operation_id: str | None = None,
    response_only: bool = False,
    status_code: int = 200,
    response_description: str = "Successful response",
    response_media_type: str = "application/json",
    responses: Mapping[int, Any] | None = None,
    deprecated: bool = False,
    deprecated_at: datetime | None = None,
    sunset_at: datetime | None = None,
    deprecation_link: str | None = None,
    include_in_schema: bool = True,
    security: Mapping[str, Iterable[str]] | None = None,
    name: str | None = None,
    host: str | None = None,
    cancel_on_disconnect: bool | None = None,
) -> RouteDefinition:
    """Normalize one route declaration into the sole immutable route record.

    `Router` and `Wreath` differ in where they store the result, not in what a
    declaration means. Keeping normalization here prevents their decorator
    surfaces, router inclusion, and startup compiler from growing independent
    interpretations of methods, permissions, responses, or security metadata.
    """
    wire_lifecycle = lifecycle_headers(
        deprecated_at=deprecated_at,
        sunset_at=sunset_at,
        deprecation_link=deprecation_link,
    )
    return RouteDefinition(
        path=path,
        methods=tuple(method.upper() for method in methods),
        endpoint=endpoint,
        middleware=tuple(middleware),
        tags=tuple(tags),
        summary=summary,
        dependencies=tuple(dependencies),
        requirement=merge_requirements(requirement, _permission_requirement(permissions)),
        operation_id=operation_id,
        response_only=response_only,
        status_code=status_code,
        response_description=response_description,
        response_media_type=response_media_type,
        responses=tuple((int(code), spec) for code, spec in (responses or {}).items()),
        deprecated=deprecated or deprecated_at is not None,
        lifecycle_headers=wire_lifecycle,
        include_in_schema=include_in_schema,
        security=tuple((key, tuple(scopes)) for key, scopes in (security or {}).items()),
        name=name,
        host=host,
        cancel_on_disconnect=cancel_on_disconnect,
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
        response_only: Handler promises to return a response object directly.
        cancel_on_disconnect: Cancel the handler when the client goes away, or
            None to let the request method decide.
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
    response_only: bool = False
    status_code: int = 200
    response_description: str = "Successful response"
    response_media_type: str = "application/json"
    responses: tuple[tuple[int, Any], ...] = ()
    deprecated: bool = False
    lifecycle_headers: tuple[tuple[bytes, bytes], ...] = ()
    include_in_schema: bool = True
    security: tuple[tuple[str, tuple[str, ...]], ...] = ()
    name: str | None = None
    host: str | None = None
    #: Whether losing the client cancels this route's handler. `None` -- the
    #: default -- defers to the request method: safe methods (`GET`, `HEAD`,
    #: `OPTIONS`, `QUERY`) are cancelled, everything else is left to finish.
    #: Setting it here overrides that either way, for every method the route
    #: answers.
    cancel_on_disconnect: bool | None = None


@dataclass(frozen=True, slots=True)
class WebSocketRouteDefinition:
    """One reusable WebSocket route with inherited access requirements."""

    path: str
    endpoint: Callable[[Any], Awaitable[None]]
    requirement: AuthRequirement = AuthRequirement()


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
        "_websockets",
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
        self._websockets: list[WebSocketRouteDefinition] = []

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

    @property
    def websockets(self) -> tuple[WebSocketRouteDefinition, ...]:
        """WebSocket routes owned by this router, including mounted snapshots."""
        return tuple(self._websockets)

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
        response_only: bool = False,
        status_code: int = 200,
        response_description: str = "Successful response",
        response_media_type: str = "application/json",
        responses: Mapping[int, Any] | None = None,
        deprecated: bool = False,
        deprecated_at: datetime | None = None,
        sunset_at: datetime | None = None,
        deprecation_link: str | None = None,
        include_in_schema: bool = True,
        security: Mapping[str, Iterable[str]] | None = None,
        name: str | None = None,
        host: str | None = None,
        cancel_on_disconnect: bool | None = None,
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
            response_only: Promise that the handler returns a response object directly.
            deprecated: Mark the operation deprecated in OpenAPI.
            deprecated_at: RFC 9745 effective date, emitted as `Deprecation`.
            sunset_at: Expected shutdown date, emitted as `Sunset`.
            deprecation_link: URI-reference for `rel="deprecation"` documentation.
            cancel_on_disconnect: Cancel this handler when the client goes away.
                Omitted, the request method decides: safe methods are cancelled,
                unsafe ones are not.

        Returns:
            A decorator that registers the handler and returns it unchanged.

        Raises:
            ValueError: The path does not begin with a slash.
        """
        full_path = _path(self._prefix, path)
        _validate_status_code(status_code)

        def register(handler: Handler) -> Handler:
            self._routes.append(
                _route_definition(
                    full_path,
                    handler,
                    methods=methods,
                    middleware=(*self._middleware, *middleware),
                    tags=(*self._tags, *tags),
                    summary=summary,
                    dependencies=(*self._dependencies, *dependencies),
                    permissions=permissions,
                    requirement=self._requirement,
                    operation_id=operation_id,
                    response_only=response_only,
                    status_code=status_code,
                    response_description=response_description,
                    response_media_type=response_media_type,
                    responses=responses,
                    deprecated=deprecated,
                    deprecated_at=deprecated_at,
                    sunset_at=sunset_at,
                    deprecation_link=deprecation_link,
                    include_in_schema=include_in_schema,
                    security=security,
                    name=name,
                    host=host,
                    cancel_on_disconnect=cancel_on_disconnect,
                )
            )
            return handler

        return register

    def get(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a handler for `path` on GET; keywords are those of `route()`."""
        return self.route(path, methods=("GET",), **metadata)

    def query(
        self,
        path: str,
        *,
        accept_query: Iterable[str] = ("application/json",),
        **metadata: Any,
    ) -> Callable[[Handler], Handler]:
        """Register a safe, idempotent QUERY route with accepted content types."""
        policy = _QueryContentPolicy(accept_query).middleware()
        middleware = tuple(metadata.pop("middleware", ()))
        return self.route(path, methods=("QUERY",), middleware=(policy, *middleware), **metadata)

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

    def websocket(
        self,
        path: str,
        *,
        permissions: Iterable[str] = (),
    ) -> Callable[[Callable[[Any], Awaitable[None]]], Callable[[Any], Awaitable[None]]]:
        """Register a reusable WebSocket route.

        Router-level and route-level permissions are enforced during the
        handshake when the router is included in an application. Message-loop
        flow control remains the handler's choice; `WebSocketService` is the
        bounded generic manager when it needs framework ownership.
        """
        full_path = _path(self._prefix, path)
        requirement = merge_requirements(self._requirement, _permission_requirement(permissions))

        def register(
            handler: Callable[[Any], Awaitable[None]],
        ) -> Callable[[Any], Awaitable[None]]:
            self._websockets.append(WebSocketRouteDefinition(full_path, handler, requirement))
            return handler

        return register

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
        inherited = merge_requirements(self._requirement, include_requirement)
        for definition in router.websockets:
            path = _path(include_prefix, definition.path)
            path = _path(self._prefix, path)
            self._websockets.append(
                WebSocketRouteDefinition(
                    path,
                    definition.endpoint,
                    merge_requirements(inherited, definition.requirement),
                )
            )


__all__ = ["RouteDefinition", "Router", "WebSocketRouteDefinition"]
