"""Wreath ASGI application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from time import monotonic_ns as _monotonic_ns
from typing import Any, Literal, cast

from ._auth.backends import AuthenticationBackend, AuthorizationProvider
from ._auth.models import Identity
from ._flight_schema import PhaseCoverage as _PhaseCoverage
from ._flight_schema import PhaseKind as _PhaseKind
from ._auth.requirements import (
    AuthRequirement,
    SetRequirement,
    merge_requirements,
    requirement_for,
)
from ._json import dumps as _json_dumps
from ._native import _core
from ._pure.authz import build_capability_mask as _pure_build_capability_mask
from ._routing import _CLASSIFYING, Handler, RoutingMode
from ._routing import Router as CompiledRouter
from .binding import Depends, ValidationError, compile_binder
from .cache_control import CacheControl
from .exceptions import Forbidden, HTTPException, Unauthorized
from .middleware.base import Middleware, MiddlewareRoute, compile_middleware
from .request import DEFAULT_LIMITS, Request, RequestLimits
from .response import (
    FileResponse,
    JSONResponse,
    PreparedResponse,
    ProblemResponse,
    Response,
    Send,
    StreamingResponse,
    TextResponse,
)
from .router import RouteDefinition, Router
from .state import State
from .websocket import WebSocket, WebSocketDisconnect

_build_capability_mask = (
    _pure_build_capability_mask if _core is None else _core.build_capability_mask
)

# Baseline Response.__call__ used to detect subclasses that override sending;
# only unmodified responses ride the one-shot "wreath.response" server extension.
_RESPONSE_CALL = Response.__call__

# Flight-recorder phase markers, pre-resolved to plain ints so the armed request
# path never touches the IntEnum machinery. Only a request whose native context
# reports `flight == 2` (recorder attached and this request sampled into
# Detailed) ever emits one; every other request skips the markers entirely.
_PH_AUTH = int(_PhaseKind.AUTH)
_PH_HANDLER = int(_PhaseKind.HANDLER)
_PH_SERIALIZE = int(_PhaseKind.SERIALIZE)
_COV_PYTHON = int(_PhaseCoverage.PYTHON)

ExceptionHandler = Callable[[Request, Exception], Awaitable[Any]]
WebSocketHandler = Callable[[WebSocket], Awaitable[None]]
LifespanHandler = Callable[["Wreath"], Awaitable[None]]


class Wreath:
    """A compact ASGI application with an intentionally provisional API."""

    __slots__ = (
        "_all_capability_mask",
        "_auth_backend",
        "_authorizer",
        "_capabilities",
        "_classify",
        "_dirty",
        "_databases",
        "_exception_handlers",
        "_flight_route_ids",
        "_flight_route_keys",
        "_limits",
        "_fallback_exception_handler",
        "_global_hooks",
        "_global_middleware",
        "_handler_requirements",
        "_http_clients",
        "_match",
        "_middleware",
        "_middleware_order",
        "_orm_registries",
        "_preflight_fallback",
        "_probe",
        "_resolve",
        "_routes",
        "_routing",
        "_shutdown_handlers",
        "_startup_handlers",
        "_stage_hooks",
        "_static_mounts",
        "_status_handlers",
        "_webhook_hubs",
        "_ws_router",
        "_ws_routes",
        "debug",
        "router",
        "state",
    )

    def __init__(
        self,
        *,
        debug: bool = False,
        routing: RoutingMode = "decision",
        limits: RequestLimits = DEFAULT_LIMITS,
    ) -> None:
        self._routing = routing
        self._limits = limits
        self.router = CompiledRouter(routing)
        # Bind the route table's match directly: it saves a Python frame per
        # request over going through Router.match. The table never changes
        # identity, so the binding stays valid as routes are added.
        self._match = self.router._table.match
        self._classify = getattr(self.router._table, "classify", None)
        self._resolve = getattr(self.router._table, "resolve", None)
        self._probe = getattr(self.router._table, "probe", None)
        self.debug = debug
        self._auth_backend: AuthenticationBackend | None = None
        self._authorizer: AuthorizationProvider | None = None
        self._capabilities: dict[str, int] = {"authenticated": 1}
        self._all_capability_mask = 1
        self._fallback_exception_handler: ExceptionHandler | None = None
        self._preflight_fallback: Callable[[Request], Awaitable[Any]] | None = None
        self.state = State()
        self._ws_router: CompiledRouter | None = None
        #: (path, handler) for each registered WebSocket route, kept beside the
        #: router so telemetry can enumerate WS routes for the metadata image and
        #: join matched handlers to their route IDs.
        self._ws_routes: list[tuple[str, WebSocketHandler]] = []
        self._static_mounts: tuple[tuple[str, Handler], ...] = ()
        self._startup_handlers: list[LifespanHandler] = []
        self._shutdown_handlers: list[LifespanHandler] = []
        self._routes: list[RouteDefinition] = []
        self._middleware: list[tuple[int, int, Middleware]] = []
        self._global_middleware: list[tuple[int, int, Middleware]] = []
        self._global_hooks: tuple[tuple[Any, Any], ...] = ()
        self._handler_requirements: dict[Any, AuthRequirement] = {}
        # Native Flight Recorder route attribution: {compiled_handler:
        # (route_id, plan_id)} joined to the Stage-0 metadata image, built lazily
        # the first time a request carries a live recorder context. None until
        # then, and reset on every route recompile so it never goes stale.
        self._flight_route_ids: dict[Any, tuple[int, int]] | None = None
        self._flight_route_keys: dict[Any, tuple[str, str]] = {}
        self._stage_hooks: dict[str, tuple[Any, ...]] = {}
        self._middleware_order = 0
        self._exception_handlers: dict[type[Exception], ExceptionHandler] = {}
        self._status_handlers: dict[int, ExceptionHandler] = {}
        self._dirty = False
        self._databases: dict[str, Any] = {}
        self._http_clients: dict[str, Any] = {}
        self._orm_registries: dict[str, Any] = {}
        self._webhook_hubs: dict[str, Any] = {}

    def webhooks(self, name: str) -> Any:
        """Register one named inbound/outbound webhook hub."""
        from .webhooks import WebhookHub

        if name in self._webhook_hubs:
            raise ValueError(f"duplicate webhook hub: {name}")
        hub = WebhookHub(self, name)
        self._webhook_hubs[name] = hub
        self.state.__setattr__(f"webhooks_{name}", hub)
        return hub

    def http_client(self, name: str, *, base_url: str, **options: Any) -> Any:
        """Register one lifespan-managed outbound HTTP client."""
        from .http_client import HTTPClient

        if name in self._http_clients:
            raise ValueError(f"duplicate HTTP client: {name}")
        client = HTTPClient(name, base_url=base_url, **options)
        self._http_clients[name] = client
        self.state.__setattr__(f"http_{name}", client)
        return client

    def postgres(
        self,
        name: str,
        *,
        dsn: str,
        pools: Any = None,
        workload_dsns: Any = None,
        shutdown_timeout: float = 10.0,
    ) -> Any:
        """Configure one application-owned PostgreSQL database."""
        from .postgres import Database

        if name in self._databases:
            raise ValueError(f"duplicate PostgreSQL database: {name}")
        database = Database(
            name,
            dsn,
            pools=pools,
            workload_dsns=workload_dsns,
            shutdown_timeout=shutdown_timeout,
        )
        self._databases[name] = database
        self.state.__setattr__(f"postgres_{name}", database)
        self._dirty = True
        return database

    def orm(
        self,
        *,
        database: str,
        models: Iterable[type],
        validate_schema: Literal["off", "warn", "error"] = "error",
        query_cache_size: int = 512,
    ) -> Any:
        """Compile ``models`` against an existing ``app.postgres()`` database.

        Models and relationships resolve immediately, so an invalid declaration
        fails here rather than on a request. Schema validation runs later,
        during lifespan startup, once the database is up.
        """
        from .orm.registry import Registry

        if database not in self._databases:
            known = ", ".join(sorted(self._databases)) or "none"
            raise ValueError(
                f"unknown PostgreSQL database: {database!r}; configured: {known}"
            )
        if database in self._orm_registries:
            raise ValueError(f"duplicate ORM registry for database: {database!r}")
        registry = Registry(
            self._databases[database],
            tuple(models),
            validate_schema=validate_schema,
            query_cache_size=query_cache_size,
        )
        self._orm_registries[database] = registry
        self.state.__setattr__(f"orm_{database}", registry)
        self._dirty = True
        return registry

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
        route_methods = tuple(method.upper() for method in methods)
        route_middleware = tuple(middleware)
        route_tags = tuple(tags)
        route_dependencies = tuple(dependencies)
        permission_values = frozenset(permissions)
        requirement = (
            AuthRequirement(
                authenticated=True,
                permission_checks=(SetRequirement(permission_values, "all"),),
            )
            if permission_values
            else AuthRequirement()
        )

        def register(handler: Handler) -> Handler:
            for method in route_methods:
                self.router.add(path, method, handler)
            self._routes.append(
                RouteDefinition(
                    path,
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
            self._dirty = True
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
        if prefix and not prefix.startswith("/"):
            raise ValueError("router prefixes must begin with '/'")
        prefix = prefix.rstrip("/")
        include_tags = tuple(tags)
        include_middleware = tuple(middleware)
        include_dependencies = tuple(dependencies)
        permission_values = frozenset(permissions)
        include_requirement = (
            AuthRequirement(
                authenticated=True,
                permission_checks=(SetRequirement(permission_values, "all"),),
            )
            if permission_values
            else AuthRequirement()
        )
        for definition in router.routes:
            path = prefix + definition.path if prefix else definition.path
            for method in definition.methods:
                self.router.add(path, method, definition.endpoint)
            self._routes.append(
                RouteDefinition(
                    path,
                    definition.methods,
                    definition.endpoint,
                    include_middleware + definition.middleware,
                    include_tags + definition.tags,
                    definition.summary,
                    include_dependencies + definition.dependencies,
                    merge_requirements(include_requirement, definition.requirement),
                    definition.operation_id,
                )
            )
        self._dirty = True

    def configure_auth(
        self,
        backend: AuthenticationBackend,
        authorizer: AuthorizationProvider | None = None,
    ) -> None:
        self._auth_backend = backend
        self._authorizer = authorizer
        self._dirty = True

    def add_middleware(self, middleware: Middleware, *, priority: int = 0) -> None:
        if getattr(middleware, "global_scope", False):
            self.add_global_middleware(middleware, priority=priority)
            return
        self._middleware.append((priority, self._middleware_order, middleware))
        self._middleware_order += 1
        self._dirty = True

    def add_global_middleware(self, middleware: Middleware, *, priority: int = 0) -> None:
        """Register hook middleware around routing and all HTTP responses.

        Global middleware must expose ``before`` and/or ``after`` hooks. Unlike
        route middleware, it covers misses, static files, and authorization
        failures, so it is suitable for ingress checks and response headers.
        """
        if not any(hasattr(middleware, name) for name in ("before", "after")):
            raise TypeError("global middleware must expose before and/or after hooks")
        self._global_middleware.append((priority, self._middleware_order, middleware))
        self._middleware_order += 1
        preflight = getattr(middleware, "handle_preflight", None)
        if preflight is not None:
            if self._preflight_fallback is not None:
                raise ValueError("only one global CORS preflight handler may be configured")
            self._preflight_fallback = preflight
        self._dirty = True

    def middleware(
        self, middleware: Middleware | None = None, *, priority: int = 0
    ) -> Middleware | Callable[[Middleware], Middleware]:
        def register(value: Middleware) -> Middleware:
            self.add_middleware(value, priority=priority)
            return value

        return register if middleware is None else register(middleware)

    def static(
        self,
        prefix: str,
        directory: str,
        *,
        html_index: bool = True,
        cache_control: CacheControl | None = None,
    ) -> None:
        """Serve files under ``directory`` for paths beginning with ``prefix``.

        Mounts are consulted only when no route matches, so they cost nothing
        on the routed hot path.
        """
        from .staticfiles import StaticFiles

        normalized = "/" + prefix.strip("/") + "/"
        handler = StaticFiles(
            directory, html_index=html_index, cache_control=cache_control
        )
        self._static_mounts = (*self._static_mounts, (normalized, cast("Handler", handler)))

    def websocket(self, path: str) -> Callable[[WebSocketHandler], WebSocketHandler]:
        """Register a WebSocket handler; it receives one WebSocket per connection."""

        def register(handler: WebSocketHandler) -> WebSocketHandler:
            if self._ws_router is None:
                self._ws_router = CompiledRouter(self._routing)
            # The router stores opaque callables; WS handlers take a
            # WebSocket where HTTP handlers take a Request.
            self._ws_router.add(path, "WEBSOCKET", cast("Handler", handler))
            self._ws_routes.append((path, handler))
            # Rebuild the flight route-key map (and drop the cached IDs) so a WS
            # route registered after HTTP compilation is still attributable.
            self._dirty = True
            return handler

        return register

    def add_exception_handler(
        self, error_type: type[Exception], handler: ExceptionHandler
    ) -> None:
        self._exception_handlers[error_type] = handler

    def exception_handler(
        self, error_type: type[Exception]
    ) -> Callable[[ExceptionHandler], ExceptionHandler]:
        def register(handler: ExceptionHandler) -> ExceptionHandler:
            self.add_exception_handler(error_type, handler)
            return handler

        return register

    def add_status_handler(self, status: int, handler: ExceptionHandler) -> None:
        self._status_handlers[status] = handler

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Send) -> None:
        if self._dirty:
            self._compile_routes()
        scope_type = scope["type"]
        if scope_type != "http":
            if scope_type == "websocket":
                await self._handle_websocket(scope, receive, send)
                return
            if scope_type == "lifespan":
                await self._lifespan(receive, send)
                return
            raise ValueError(f"unsupported ASGI scope: {scope_type!r}")
        await self._handle_http(
            scope, receive, send, scope["method"], scope["path"], False
        )

    async def _wreath_http(self, context: Any, receive: Any, send: Send) -> None:
        """Wreath-server entry point using a lazily materialized ASGI scope."""
        if self._dirty:
            self._compile_routes()
        await self._handle_http(
            context, receive, send, context.method, context.path, True
        )

    async def _handle_http(
        self,
        scope: Any,
        receive: Any,
        send: Send,
        method: str,
        path: str,
        native_response: bool,
    ) -> None:
        request: Request | None = None
        global_hooks = self._global_hooks
        active_global = len(global_hooks)
        if global_hooks:
            request = Request(scope, receive, limits=self._limits)
            request._route_outcome = "ingress"
            for index, (before, _after) in enumerate(global_hooks):
                if before is None:
                    continue
                try:
                    candidate = await before(request)
                except Exception as error:
                    candidate = await self._handle_exception(request, error)
                if candidate is not None:
                    active_global = index + 1
                    await self._finish_http(
                        request,
                        _coerce_response(candidate),
                        send,
                        method,
                        scope,
                        native_response,
                        active_global,
                    )
                    return

        if self._routing in _CLASSIFYING:
            classify = self._classify
            resolve = self._resolve
            assert classify is not None and resolve is not None
            classification, payload = classify(method, path)
            matched = payload if classification == 1 else None
            if classification == 2:
                ticket = payload
                if request is None:
                    request = Request(scope, receive, limits=self._limits)
                backend = self._auth_backend
                if backend is None:
                    if global_hooks:
                        request._set_route_outcome("protected")
                    response = await self._handle_exception(request, Unauthorized())
                    await self._finish_http(
                        request, response, send, method, scope, native_response, active_global
                    )
                    return
                stage_response = (
                    await self._run_stage("pre_auth", request)
                    if "pre_auth" in self._stage_hooks
                    else None
                )
                if stage_response is not None:
                    await self._finish_http(
                        request,
                        stage_response,
                        send,
                        method,
                        scope,
                        native_response,
                        active_global,
                    )
                    return
                # Armed-only AUTH phase. `flight == 2` only on the native path
                # with a recorder attached and this request sampled into
                # Detailed; every other request pays just the member read.
                auth_start = (
                    _monotonic_ns() if native_response and scope.flight == 2 else 0
                )
                identity = await backend.authenticate(request)
                if auth_start:
                    scope._flight_phase(
                        _PH_AUTH, 0, _COV_PYTHON, _monotonic_ns() - auth_start
                    )
                request._set_identity(identity)
                stage_response = (
                    await self._run_stage("identity", request)
                    if "identity" in self._stage_hooks
                    else None
                )
                if stage_response is not None:
                    await self._finish_http(
                        request,
                        stage_response,
                        send,
                        method,
                        scope,
                        native_response,
                        active_global,
                    )
                    return
                caller_mask = self._identity_mask(identity)
                matched = resolve(ticket, caller_mask)
                if matched is None:
                    error: HTTPException
                    if identity is None:
                        error = Unauthorized(challenge=backend.challenge(request))
                    else:
                        error = Forbidden("Forbidden")
                    if global_hooks:
                        request._set_route_outcome("protected")
                    response = await self._handle_exception(request, error)
                    await self._finish_http(
                        request, response, send, method, scope, native_response, active_global
                    )
                    return
        else:
            # Trie routes are selected without capability filtering; the common
            # authorization stage below checks the compiled route requirement.
            matched = self._route_match(method, path, 0)
        if matched is None:
            if request is None:
                request = Request(scope, receive, limits=self._limits)
            if global_hooks:
                request._set_route_outcome("miss")
            stage_response = (
                await self._run_stage("miss", request)
                if "miss" in self._stage_hooks
                else None
            )
            if stage_response is not None:
                await self._finish_http(
                    request,
                    stage_response,
                    send,
                    method,
                    scope,
                    native_response,
                    active_global,
                )
                return
            if method == "OPTIONS" and self._preflight_fallback is not None:
                preflight_response = await self._preflight_fallback(request)
                if preflight_response is not None:
                    await self._finish_http(
                        request,
                        _coerce_response(preflight_response),
                        send,
                        method,
                        scope,
                        native_response,
                        active_global,
                    )
                    return
            for mount_prefix, static_handler in self._static_mounts:
                if path.startswith(mount_prefix):
                    matched = (static_handler, {"path": path[len(mount_prefix):]})
                    if global_hooks:
                        request._set_route_outcome("static")
                    break
            else:
                response = ProblemResponse(status=404, detail="Not Found")
                await self._finish_http(
                    request, response, send, method, scope, native_response, active_global
                )
                return
        handler, path_params = matched
        # Attribute this completion to its route in the recorder. Only the native
        # HTTP/1 fast path (native_response) carries a _RequestContext, and its
        # `flight` flag is a T_INT member that is truthy only when a live recorder
        # context is attached -- so native Off skips with no new crossing, and the
        # stamp rides the context that already crossed into C.
        #
        # HTTP/2 and HTTP/3 dispatch through the dict-scope path (no
        # _RequestContext). Their native protocol seeds `_wreath_flight` into the
        # scope only when a recorder is attached; we overwrite it with the
        # (route_id, plan_id) tuple, which C reads from the retained scope at
        # completion before it emits the cell. A pure ASGI server (uvicorn) never
        # seeds the key, so it pays only one dict membership test and no crossing.
        flight_phase = None
        if native_response:
            flight = scope.flight
            if flight:
                ids = self._flight_route_ids or self._build_flight_route_ids()
                attribution = ids.get(handler)
                if attribution is not None:
                    scope._flight_stamp(attribution[0], attribution[1])
                if flight == 2:
                    # Armed for Detailed capture: bind the phase marker once so
                    # the handler/serialize timing below is a plain local call.
                    flight_phase = scope._flight_phase
        elif "_wreath_flight" in scope:
            ids = self._flight_route_ids or self._build_flight_route_ids()
            attribution = ids.get(handler)
            if attribution is not None:
                scope["_wreath_flight"] = attribution
        if request is None:
            request = Request(scope, receive, path_params, self._limits)
        else:
            request.path_params = path_params or {}
        if global_hooks and request._get_route_outcome() in (None, "ingress"):
            request._set_route_outcome("route")
        requirement = self._handler_requirements.get(handler)
        if requirement is not None and requirement.authenticated:
            try:
                if flight_phase is None:
                    stage_response = await self._authorize_request(request, requirement)
                else:
                    auth_start = _monotonic_ns()
                    stage_response = await self._authorize_request(request, requirement)
                    flight_phase(
                        _PH_AUTH, 0, _COV_PYTHON, _monotonic_ns() - auth_start
                    )
            except Exception as error:
                response = await self._handle_exception(request, error)
                await self._finish_http(
                    request, response, send, method, scope, native_response, active_global
                )
                return
            if stage_response is not None:
                await self._finish_http(
                    request,
                    stage_response,
                    send,
                    method,
                    scope,
                    native_response,
                    active_global,
                )
                return
        stage_response = (
            await self._run_stage("action", request)
            if "action" in self._stage_hooks
            else None
        )
        if stage_response is not None:
            await self._finish_http(
                request, stage_response, send, method, scope, native_response, active_global
            )
            return
        try:
            if flight_phase is None:
                value = await handler(request)
                response = _coerce_response(value)
            else:
                handler_start = _monotonic_ns()
                value = await handler(request)
                flight_phase(
                    _PH_HANDLER, 0, _COV_PYTHON, _monotonic_ns() - handler_start
                )
                serialize_start = _monotonic_ns()
                response = _coerce_response(value)
                flight_phase(
                    _PH_SERIALIZE, 0, _COV_PYTHON, _monotonic_ns() - serialize_start
                )
        except Exception as error:
            response = await self._handle_exception(request, error)

        await self._finish_http(
            request, response, send, method, scope, native_response, active_global
        )

    async def _finish_http(
        self,
        request: Request,
        response: Response | StreamingResponse | FileResponse | PreparedResponse,
        send: Send,
        method: str,
        scope: Any,
        native_response: bool,
        active_global: int,
    ) -> None:
        hooks = self._global_hooks
        for _before, after in reversed(hooks[:active_global]) if active_global else ():
            if after is not None:
                try:
                    response = _coerce_response(await after(request, response))
                except Exception as error:
                    response = await self._handle_exception(request, error)

        extensions = None if native_response else scope.get("extensions")
        if method == "HEAD":
            await response(_head_send(send))
        elif type(response).__call__ is _RESPONSE_CALL and (
            native_response or (extensions is not None and "wreath.response" in extensions)
        ):
            plain = cast(Response, response)
            await send(
                {
                    "type": "wreath.response",
                    "status": plain.status,
                    "headers": plain.headers,
                    "body": plain.body,
                }
            )
        else:
            await response(send)
        background = response.background
        if background is not None:
            await background()

    async def _handle_websocket(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        router = self._ws_router
        matched = (
            router.match("WEBSOCKET", scope["path"]) if router is not None else (None, None)
        )
        handler, path_params = matched
        if handler is None:
            # No route: reject the handshake (the server answers with 403).
            await send({"type": "websocket.close", "code": 1000})
            return
        # Attribute this session to its WEBSOCKET route in the recorder. The
        # native protocol seeds `_wreath_flight` into the scope only when a
        # recorder is attached, so a pure ASGI server pays one membership test and
        # no crossing; C reads the tuple off the retained scope at completion.
        if "_wreath_flight" in scope:
            ids = self._flight_route_ids or self._build_flight_route_ids()
            attribution = ids.get(handler)
            if attribution is not None:
                scope["_wreath_flight"] = attribution
        websocket = WebSocket(scope, receive, send, path_params)
        try:
            await cast("WebSocketHandler", handler)(websocket)
        except WebSocketDisconnect:
            # The peer left; nothing further to send.
            return

    async def _handle_exception(
        self, request: Request, error: Exception
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse:
        for error_type in type(error).__mro__:
            handler = self._exception_handlers.get(error_type)
            if handler is not None:
                return _coerce_response(await handler(request, error))
        if isinstance(error, ValidationError):
            return ProblemResponse(
                status=422,
                detail="Request validation failed",
                extensions={"errors": error.errors},
            )
        if isinstance(error, HTTPException):
            handler = self._status_handlers.get(error.status)
            if handler is not None:
                return _coerce_response(await handler(request, error))
            return ProblemResponse(
                status=error.status,
                detail=error.detail,
                headers=error.headers,
            )
        if self._fallback_exception_handler is not None:
            return _coerce_response(await self._fallback_exception_handler(request, error))
        if self.debug:
            return ProblemResponse(
                status=500,
                detail=f"{type(error).__name__}: {error}",
            )
        return ProblemResponse(status=500, detail="Internal Server Error")

    def _compile_routes(self) -> None:
        router = CompiledRouter(self._routing)
        app_middleware = tuple(
            item[2] for item in sorted(self._middleware, key=lambda item: (item[0], item[1]))
        )
        global_middleware = tuple(
            item[2]
            for item in sorted(self._global_middleware, key=lambda item: (item[0], item[1]))
        )
        self._global_hooks = tuple(
            (getattr(item, "before", None), getattr(item, "after", None))
            for item in global_middleware
        )
        stage_hooks = {
            stage: tuple(
                hook
                for item in global_middleware
                if (hook := getattr(item, stage, None)) is not None
            )
            for stage in ("miss", "pre_auth", "identity", "action")
        }
        self._stage_hooks = {stage: hooks for stage, hooks in stage_hooks.items() if hooks}
        requirements = [
            merge_requirements(route.requirement, requirement_for(route.endpoint))
            for route in self._routes
        ]
        self._compile_capabilities(requirements)
        handler_requirements: dict[Any, AuthRequirement] = {}
        # Compiled handler -> (method, path) so a later telemetry join can map
        # each route to its metadata-image IDs without re-walking the router.
        flight_route_keys: dict[Any, tuple[str, str]] = {}
        for definition, requirement in zip(self._routes, requirements, strict=True):
            # Typed-signature binding compiles once here; request-only
            # handlers come back unchanged.
            endpoint = compile_binder(
                definition.endpoint,
                definition.path,
                databases=self._databases,
                orm_registries=self._orm_registries,
                dependencies=definition.dependencies,
            )
            chain = app_middleware + definition.middleware
            if chain:
                # Middleware (hooks and call_next alike) always sees Response
                # objects, never raw handler return values; without a chain
                # the coercion in __call__ suffices.
                endpoint = _ensure_response(endpoint)
            access_clauses = self._requirement_clauses(requirement)
            for method in definition.methods:
                compiled = (
                    endpoint
                    if not chain
                    else compile_middleware(
                        endpoint,
                        chain,
                        route=MiddlewareRoute(
                            definition.path,
                            method,
                            definition.endpoint,
                            authenticated=requirement.authenticated,
                        ),
                    )
                )
                router.add(definition.path, method, compiled, access_clauses)
                handler_requirements[compiled] = requirement
                flight_route_keys[compiled] = (method, definition.path)
        # WebSocket routes are attributable too: the matched handler joins to a
        # WEBSOCKET route in the metadata image. They carry no HTTP plan.
        for ws_path, ws_handler in self._ws_routes:
            flight_route_keys[ws_handler] = ("WEBSOCKET", ws_path)
        self._handler_requirements = handler_requirements
        self._flight_route_keys = flight_route_keys
        self._flight_route_ids = None  # rebuilt lazily against the new routes
        self.router = router
        self._match = router._table.match
        self._classify = getattr(router._table, "classify", None)
        self._resolve = getattr(router._table, "resolve", None)
        self._probe = getattr(router._table, "probe", None)
        self._dirty = False

    def _build_flight_route_ids(self) -> dict[Any, tuple[int, int]]:
        """Join compiled handlers to their metadata-image route/plan IDs.

        Built once, only when telemetry is actually recording (the request
        carried a live recorder context), so a non-telemetry app never pays the
        metadata-image inspection cost. The IDs are exactly the Stage-0 image's,
        so completion cells attribute to the same route table the Inspector and
        exporters read.
        """
        from ._flight_metadata import build_metadata_image

        image = build_metadata_image(self)
        by_route: dict[tuple[str, str], tuple[int, int]] = {
            (route.method, route.path): (route.route_id, route.plan_id)
            for route in image.routes
        }
        mapping: dict[Any, tuple[int, int]] = {
            handler: ids
            for handler, key in self._flight_route_keys.items()
            if (ids := by_route.get(key)) is not None
        }
        self._flight_route_ids = mapping
        return mapping

    def _route_match(self, method: str, path: str, caller_mask: int) -> Any:
        match: Any = self._match
        if self._routing in _CLASSIFYING:
            return match(method, path, caller_mask)
        return match(method, path)

    def _compile_capabilities(self, requirements: list[AuthRequirement]) -> None:
        names = {"authenticated"}
        for requirement in requirements:
            for check in requirement.role_checks:
                names.update(f"role:{value}" for value in check.values)
            for check in requirement.permission_checks:
                names.update(f"permission:{value}" for value in check.values)
        self._capabilities = {
            name: 1 << index for index, name in enumerate(sorted(names))
        }
        self._all_capability_mask = (1 << len(self._capabilities)) - 1

    def _requirement_clauses(self, requirement: AuthRequirement) -> tuple[int, ...]:
        clauses = [self._capabilities["authenticated"] if requirement.authenticated else 0]
        for prefix, checks in (
            ("role", requirement.role_checks),
            ("permission", requirement.permission_checks),
        ):
            for check in checks:
                masks = [
                    self._capabilities[f"{prefix}:{value}"] for value in check.values
                ]
                if check.mode == "all":
                    combined = 0
                    for mask in masks:
                        combined |= mask
                    clauses = [clause | combined for clause in clauses]
                else:
                    clauses = [clause | mask for clause in clauses for mask in masks]
        return tuple(dict.fromkeys(clauses))

    def _identity_mask(self, identity: Identity | None) -> int:
        if identity is None:
            return 0
        return _build_capability_mask(self._capabilities, identity.roles, identity.permissions)

    async def _run_stage(
        self, stage: str, request: Request
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse | None:
        for hook in self._stage_hooks.get(stage, ()):
            try:
                candidate = await hook(request)
            except Exception as error:
                return await self._handle_exception(request, error)
            if candidate is not None:
                return _coerce_response(candidate)
        return None

    async def _authorize_request(
        self, request: Request, requirement: AuthRequirement
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse | None:
        """Run authentication and authorization before route middleware."""
        identity = request.identity
        if identity is None:
            if "pre_auth" in self._stage_hooks:
                stage_response = await self._run_stage("pre_auth", request)
                if stage_response is not None:
                    return stage_response
            backend = self._auth_backend
            if backend is not None:
                identity = await backend.authenticate(request)
                request._set_identity(identity)
                if "identity" in self._stage_hooks:
                    stage_response = await self._run_stage("identity", request)
                    if stage_response is not None:
                        return stage_response
        if identity is None:
            challenge = (
                None
                if self._auth_backend is None
                else self._auth_backend.challenge(request)
            )
            raise Unauthorized(challenge=challenge)
        for check in requirement.role_checks:
            if not _check_set(identity.roles, check):
                raise Forbidden("Forbidden")
        for check in requirement.permission_checks:
            if not _check_set(identity.permissions, check):
                raise Forbidden("Forbidden")
        if requirement.policies:
            authorizer = self._authorizer
            if authorizer is None:
                raise RuntimeError("authorization provider is not configured")
            for policy in requirement.policies:
                decision = await authorizer.authorize(request, policy)
                if not decision.allowed:
                    raise Forbidden(decision.reason or "Forbidden")
        return None

    def enable_docs(
        self,
        *,
        docs_path: str = "/docs",
        spec_path: str = "/openapi.json",
        title: str = "Wreath",
        version: str = "0.1.0",
    ) -> None:
        """Serve the OpenAPI document and a Swagger UI page for this app.

        The document is generated once, on first request, from the same
        signature inspection that drives request binding.
        """
        from .openapi import docs_page, generate_openapi
        from .response import HTMLResponse

        spec_cache: list[bytes] = []
        page = HTMLResponse(docs_page(title, spec_path))

        @self.get(spec_path)
        async def openapi_spec(request: Request) -> Any:
            if not spec_cache:
                spec_cache.append(
                    _json_dumps(generate_openapi(self, title=title, version=version))
                )
            return Response(spec_cache[0], media_type=b"application/json")

        @self.get(docs_path)
        async def docs(request: Request) -> Any:
            return page

    def on_startup(self, handler: LifespanHandler) -> LifespanHandler:
        """Run ``handler(app)`` during lifespan startup, in registration order."""
        self._startup_handlers.append(handler)
        return handler

    def on_shutdown(self, handler: LifespanHandler) -> LifespanHandler:
        """Run ``handler(app)`` during lifespan shutdown, in registration order."""
        self._shutdown_handlers.append(handler)
        return handler

    async def _lifespan(self, receive: Any, send: Send) -> None:
        while True:
            message = await receive()
            message_type = message["type"]
            if message_type == "lifespan.startup":
                started_databases: list[Any] = []
                started_clients: list[Any] = []
                try:
                    for database in self._databases.values():
                        await database.start()
                        started_databases.append(database)
                    if self._orm_registries:
                        # Ordered explicitly rather than through a synthetic
                        # startup handler: the schema must be checked once the
                        # database is up but before user code queries it.
                        from .orm.introspection import validate_registry

                        for registry in self._orm_registries.values():
                            await validate_registry(registry)
                    for client in self._http_clients.values():
                        await client.start()
                        started_clients.append(client)
                    for handler in self._startup_handlers:
                        await handler(self)
                except Exception as error:  # noqa: BLE001 - reported to the server
                    for client in reversed(started_clients):
                        await client.close()
                    for database in reversed(started_databases):
                        await database.stop()
                    await send(
                        {"type": "lifespan.startup.failed", "message": f"{error!r}"}
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                try:
                    for handler in self._shutdown_handlers:
                        await handler(self)
                    for client in reversed(tuple(self._http_clients.values())):
                        await client.close()
                    for database in reversed(tuple(self._databases.values())):
                        await database.stop()
                except Exception as error:  # noqa: BLE001 - reported to the server
                    await send(
                        {"type": "lifespan.shutdown.failed", "message": f"{error!r}"}
                    )
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return


def _check_set(actual: frozenset[str], requirement: SetRequirement) -> bool:
    if requirement.mode == "all":
        return requirement.values <= actual
    return not requirement.values.isdisjoint(actual)


_NOT_FOUND = ProblemResponse(status=404, detail="Not Found")


def _ensure_response(endpoint: Handler) -> Handler:
    async def coerced(
        request: Request,
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse:
        return _coerce_response(await endpoint(request))

    coerced.__name__ = getattr(endpoint, "__name__", "coerced")
    coerced.__qualname__ = getattr(endpoint, "__qualname__", "coerced")
    return coerced


def _coerce_response(value: Any) -> Response | StreamingResponse | FileResponse | PreparedResponse:
    kind = type(value)
    if kind is dict:
        return JSONResponse(value)
    if kind is str:
        return TextResponse(value)
    if kind is bytes:
        return Response(value)
    if isinstance(value, (Response, StreamingResponse, FileResponse, PreparedResponse)):
        return value
    if isinstance(value, bytes):
        return Response(value)
    if isinstance(value, str):
        return TextResponse(value)
    if isinstance(value, (dict, list, tuple, int, float, bool)) or value is None:
        return JSONResponse(value)
    raise TypeError(f"handlers must return a response-compatible value, got {type(value).__name__}")


def _head_send(send: Send) -> Send:
    async def send_without_body(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            message = {**message, "body": b""}
        await send(message)

    return send_without_body
