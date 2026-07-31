"""The `Wreath` application: registration, startup compilation, and dispatch.

An application is an ordinary ASGI 3 callable. Routes, middleware, exception
handlers, lifespan handlers, and infrastructure (databases, job runners, object
stores) are registered on the instance; the first request compiles them once into
the route table, the per-handler binders, the middleware tape, and the capability
masks that dispatch actually uses. Registering anything afterwards is allowed and
marks the application dirty, so the next request recompiles.
"""

from __future__ import annotations

import re
import threading

# Imported by name, like `monotonic_ns` below: one global lookup, and it is only
# reached by a response that carries background tasks.
from asyncio import timeout as _asyncio_timeout
from collections.abc import Awaitable, Callable, Iterable, Mapping
from inspect import isawaitable
from time import monotonic_ns as _monotonic_ns
from time import time as _wall_clock
from typing import Any, Literal, cast
from urllib.parse import quote

from ._auth.backends import AuthenticationBackend, AuthorizationProvider
from ._auth.models import Identity
from ._auth.requirements import (
    AuthRequirement,
    SetRequirement,
    merge_requirements,
    requirement_for,
    second_factor_age,
)
from ._codecs import parse_qs as _parse_qs
from ._flight_markers import capture_marker as _capture_marker
from ._flight_markers import phase_marker as _phase_marker
from ._flight_schema import CaptureDisposition as _CaptureDisposition
from ._flight_schema import CaptureFieldClass as _CaptureFieldClass
from ._flight_schema import PhaseCoverage as _PhaseCoverage
from ._flight_schema import PhaseKind as _PhaseKind
from ._json import dumps as _json_dumps
from ._native import _core
from ._pure.authz import build_capability_mask as _pure_build_capability_mask
from ._routing import _CLASSIFYING, Handler, RoutingMode, check_placeholders
from ._routing import Router as CompiledRouter
from .binding import (
    AppScope,
    BindingSpec,
    Depends,
    ValidationError,
    compile_binder,
    compile_response_validator,
    inspect_handler,
)
from .cache_control import CacheControl
from .exceptions import (
    BadRequest,
    Forbidden,
    HTTPException,
    MethodNotAllowed,
    NotFound,
    Unauthorized,
)
from .logging import begin_request_for as _log_begin_request
from .logging import begin_request_seeded as _log_begin_seeded
from .logging import finish_request_for as _log_finish_request
from .logging import finish_session as _log_finish_session
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
    coerce_bytes,
    coerce_json,
    coerce_text,
)
from .router import RouteDefinition, Router
from .state import State
from .typegen.inspect import _return_annotation
from .websocket import WebSocket, WebSocketDisconnect

_build_capability_mask = (
    _pure_build_capability_mask if _core is None else _core.build_capability_mask
)

# Baseline Response.__call__ used to detect subclasses that override sending;
# only unmodified responses ride the one-shot "wreath.response" server extension.
_RESPONSE_CALL = Response.__call__


def _ambiguous_request_path(scope: dict[str, Any], path: str) -> bool:
    """Whether proxy and application could route encoded separators differently."""
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes) and b"%" in raw_path:
        lowered = raw_path.lower()
        if b"%2f" in lowered or b"%5c" in lowered:
            return True
    return "\\" in path


# Flight-recorder phase markers, pre-resolved to plain ints so the armed request
# path never touches the IntEnum machinery. Only a request whose native context
# reports `flight == 2` (recorder attached and this request sampled into
# Detailed) ever emits one; every other request skips the markers entirely.
_PH_AUTH = int(_PhaseKind.AUTH)
_PH_HANDLER = int(_PhaseKind.HANDLER)
_PH_SERIALIZE = int(_PhaseKind.SERIALIZE)
_COV_PYTHON = int(_PhaseCoverage.PYTHON)

# Forensic capture field classes, pre-resolved to plain ints. Only a Forensic
# server sets `_flight_capture_plan`, and only an armed request with an active
# capture arm ever reaches the capture scan, so this stays off every common path.
_CAP_REQUEST_HEADER = int(_CaptureFieldClass.REQUEST_HEADER)
_CAP_RESPONSE_HEADER = int(_CaptureFieldClass.RESPONSE_HEADER)
_CAP_QUERY_PARAM = int(_CaptureFieldClass.QUERY_PARAM)
_CAP_REQUEST_BODY = int(_CaptureFieldClass.REQUEST_BODY)
_CAP_RESPONSE_BODY = int(_CaptureFieldClass.RESPONSE_BODY)
_FC_REQUEST_BODY = _CaptureFieldClass.REQUEST_BODY
_FC_RESPONSE_BODY = _CaptureFieldClass.RESPONSE_BODY
_CAP_DISP_RAW = _CaptureDisposition.RAW


def _capture_body(
    capture: Any, field_class: int, rule: tuple[Any, int], data: bytes | bytearray
) -> None:
    """Hand a body to the native capture with its policy byte cap. The full body
    goes in so the native side records the true original length (and marks a RAW
    field truncated when the cap clips it); HASHED/LENGTH store only a digest or
    the length, never the bytes."""
    disposition, limit = rule
    max_bytes = limit if disposition is _CAP_DISP_RAW else 0
    capture(field_class, 0, int(disposition), bytes(data), max_bytes)


# Per-arm narrowing: each active arm carries its own compiled plan, already
# bounded by the startup ceiling. The effective decision for a field is the
# union across the active arms -- the *most-revealing* disposition any of them
# grants, which stays within the ceiling because every arm does. Descriptor ids
# still come from the ceiling plan (that is the numbering the recording's name
# table was written with); only the disposition and the capture set narrow.


def _narrow_header(arms: Any, name: str) -> Any:
    """The most-revealing header disposition any active arm grants, or None."""
    best = None
    for arm in arms:
        rule = arm.compiled.header(name)
        if rule is not None and (best is None or rule.disposition.value < best.value):
            best = rule.disposition
    return best


def _narrow_query(arms: Any, name: str) -> Any:
    """The most-revealing query-param disposition any active arm grants, or None."""
    best = None
    for arm in arms:
        rule = arm.compiled.query(name)
        if rule is not None and (best is None or rule.disposition.value < best.value):
            best = rule.disposition
    return best


def _narrow_body(arms: Any, field_class: Any) -> tuple[Any, int] | None:
    """The most-revealing (disposition, max_bytes) for a body field class, or None."""
    best: tuple[Any, int] | None = None
    for arm in arms:
        rule = arm.compiled.body(field_class)
        if rule is not None and (best is None or rule[0].value < best[0].value):
            best = rule
    return best


def _narrow_dependency(arms: Any) -> tuple[Any, int] | None:
    """The most-revealing (disposition, max_bytes) for dependency payloads, or None."""
    best: tuple[Any, int] | None = None
    for arm in arms:
        rule = arm.compiled.dependency()
        if rule is not None and (best is None or rule[0].value < best[0].value):
            best = rule
    return best


def _make_dependency_capturer(scope: Any, rule: tuple[Any, int]) -> Any:
    """Bind a dependency capturer over the request's native context and the
    narrowed dependency rule. Called only for a Forensic-armed request whose
    active arms permit dependency payloads; the returned callable takes
    `(field_class, data)` and is propagated via `capture_marker` to the
    PostgreSQL and HTTP-client seams."""
    disposition, limit = rule
    disposition_int = int(disposition)
    max_bytes = limit if disposition is _CAP_DISP_RAW else 0
    capture = scope._flight_capture

    def _capture_dependency(field_class: int, data: bytes | bytearray) -> None:
        capture(field_class, 0, disposition_int, bytes(data), max_bytes)

    return _capture_dependency


def _first_session_publisher(middleware: Iterable[Any]) -> Any | None:
    """The first item in `middleware` that publishes `request.state.session`.

    Keyed on the `publishes_session` attribute rather than on
    `SessionMiddleware` itself, so a replacement session middleware is covered
    by `_reject_session_after_authentication` too.
    """
    for item in middleware:
        if getattr(item, "publishes_session", False):
            return item
    return None


#: Most distinct access clauses one route may compile to. See
#: `_requirement_clauses`. Every clause is scanned on every request by
#: `_eligible`, so this bounds request-path work as well as declaration-time
#: memory.
MAX_ACCESS_CLAUSES = 64

#: Environments in which `enable_docs()` publishes the docs page and the
#: OpenAPI document. Anything not named here -- including the default,
#: "production" -- gets neither route and no existence leak.
NON_PRODUCTION_ENVIRONMENTS: tuple[str, ...] = (
    "dev", "development", "local", "test", "testing",
)

ExceptionHandler = Callable[[Request, Exception], Awaitable[Any]]
WebSocketHandler = Callable[[WebSocket], Awaitable[None]]
LifespanHandler = Callable[["Wreath"], Awaitable[None]]


class _ApplicationImage:
    """Immutable-route analysis shared by runtime and control-plane consumers."""

    __slots__ = ("_analyzed", "_binding_specs", "_owner", "_routes")

    def __init__(self, owner: Wreath) -> None:
        self._owner = owner
        self._routes: tuple[RouteDefinition, ...] = ()
        self._binding_specs: tuple[BindingSpec | None, ...] = ()
        self._analyzed = False

    def routes(self) -> tuple[RouteDefinition, ...]:
        current = tuple(self._owner._routes)
        if current != self._routes:
            self._routes = current
            self._binding_specs = ()
            self._analyzed = False
        return self._routes

    def binding_specs(self) -> tuple[BindingSpec | None, ...]:
        routes = self.routes()
        if not self._analyzed:
            self._binding_specs = tuple(
                inspect_handler(definition.endpoint, definition.path, definition.host)
                for definition in routes
            )
            self._analyzed = True
        return self._binding_specs


class _StaticMatcher:
    """Mount prefixes in registration order; the first that matches wins.

    Precedence is first-registration, not longest-prefix, so scanning the
    mounts in order and taking the first hit is exact -- the scan stops at the
    winner rather than having to see every candidate.

    That does mean a broad mount registered before a narrower one shadows it
    (`/assets/` before `/assets/images/` leaves the second unreachable). It is
    the documented rule rather than an oversight -- two tests in
    `tests/test_app.py` pin it, one of them named for it -- and the ordering is
    the application's to choose. Register the narrower mount first.

    This was a character trie, which is asymptotically better in the mount
    count but pays one *Python* loop iteration and dict lookup per path
    character, bounded by the longest registered prefix. `str.startswith` does
    the same comparison in one C call, so at realistic mount counts (apps mount
    a handful of directories, not hundreds) the scan is the cheaper shape.
    The trade is pinned by the `static-mount-match-scale` complexity probe.
    """

    __slots__ = ("_mounts",)

    def __init__(self) -> None:
        #: (prefix, handler) in registration order -- the match precedence.
        self._mounts: list[tuple[str, Handler]] = []

    def add(self, prefix: str, handler: Handler) -> None:
        # A repeated prefix keeps the first registration, as the trie did.
        for existing, _handler in self._mounts:
            if existing == prefix:
                return
        self._mounts.append((prefix, handler))

    def match(self, path: str) -> tuple[Handler, dict[str, str]] | None:
        for prefix, handler in self._mounts:
            if path == prefix.rstrip("/"):
                return handler, {"path": ""}
            if path.startswith(prefix):
                return handler, {"path": path[len(prefix):]}
        return None


def _dynamic_path_pattern(path: str) -> re.Pattern[str]:
    pieces: list[str] = []
    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            declaration = segment[1:-1]
            name, separator, converter = declaration.partition(":")
            expression = ".*" if separator and converter == "path" else "[^/]+"
            pieces.append(f"(?P<{name}>{expression})")
        else:
            pieces.append(re.escape(segment))
    return re.compile("^" + "/".join(pieces) + "$")


def _dynamic_host_pattern(host: str | None) -> re.Pattern[str] | None:
    if host is None:
        return None
    pieces: list[str] = []
    for segment in host.lower().split("."):
        if segment == "*":
            pieces.append("[^.]+")
        elif segment.startswith("{") and segment.endswith("}"):
            pieces.append(f"(?P<{segment[1:-1]}>[^.]+)")
        else:
            pieces.append(re.escape(segment))
    return re.compile("^" + r"\.".join(pieces) + "$")


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end >= 0 else value
    name, separator, port = value.rpartition(":")
    return name if separator and port.isdigit() else value


_ROUTE_PARAMETER = re.compile(r"\{([^}:]+)(?::(path))?\}")


def _render_route_template(template: str, values: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name, converter = match.groups()
        if name not in values:
            raise KeyError(f"missing route parameter {name!r}")
        return quote(str(values[name]), safe="/" if converter == "path" else "")

    return _ROUTE_PARAMETER.sub(replace, template)


class _DynamicMatcher:
    """Ordered fallback for host routes and trailing `{name:path}` routes."""

    __slots__ = ("_routes",)

    def __init__(self) -> None:
        self._routes: list[
            tuple[str, re.Pattern[str], re.Pattern[str] | None, Handler]
        ] = []

    def add(self, path: str, method: str, host: str | None, handler: Handler) -> None:
        self._routes.append(
            (method, _dynamic_path_pattern(path), _dynamic_host_pattern(host), handler)
        )

    def match(
        self,
        method: str,
        path: str,
        host: str,
        *,
        host_routes: bool | None = None,
    ) -> tuple[Handler, dict[str, str]] | None:
        for route_method, path_pattern, host_pattern, handler in self._routes:
            if route_method != method and not (method == "HEAD" and route_method == "GET"):
                continue
            if host_routes is not None and (host_pattern is not None) != host_routes:
                continue
            host_match = host_pattern.fullmatch(host) if host_pattern is not None else None
            if host_pattern is not None and host_match is None:
                continue
            path_match = path_pattern.fullmatch(path)
            if path_match is None:
                continue
            params = path_match.groupdict()
            if host_match is not None:
                params.update(host_match.groupdict())
            return handler, params
        return None


class _MountedResponse(Response):
    """Defer one child ASGI HTTP application until response emission."""

    __slots__ = ("_application", "_receive", "_scope")

    def __init__(self, application: Any, scope: dict[str, Any], receive: Any) -> None:
        super().__init__(b"", headers=(), media_type=b"")
        # A mount has no response metadata until the child sends its start
        # event. Parent egress middleware may append or replace headers on this
        # proxy; keeping the generated empty-body content-length would overwrite
        # the child's real length when those edits are merged below.
        self.headers = []
        self._application = application
        self._scope = scope
        self._receive = receive

    async def __call__(self, send: Send) -> None:
        async def parent_send(message: dict[str, Any]) -> None:
            if message.get("type") != "http.response.start":
                await send(message)
                return
            parent_names = {name.lower() for name, _value in self.headers}
            child_headers = [
                pair
                for pair in message.get("headers", ())
                if pair[0].lower() not in parent_names
            ]
            child_headers.extend(self.headers)
            forwarded = dict(message)
            forwarded["headers"] = child_headers
            if self.status != 200:
                forwarded["status"] = self.status
            await send(forwarded)

        await self._application(self._scope, self._receive, parent_send)


class Wreath:
    """An ASGI application: the registration surface and the request dispatcher.

    An instance is an ordinary ASGI 3 callable, so it runs on any conforming server
    as well as on wreath's own. Everything belongs to the instance -- routes,
    middleware, handlers, and the databases, job runners, message buses, HTTP
    clients and object stores registered on it. None of it is a module global, so
    two applications in one process share no state and tear down independently.

    Registration only records. The first request compiles the route table, the
    per-handler binders, the middleware tapes and the capability masks in one pass,
    and no request afterwards pays for introspection. Registering anything later
    marks the application dirty and the next request recompiles.

    Five attributes are public. `state` is a namespace for application-owned
    objects, where each registration helper publishes its result -- `postgres("main")`
    puts its database on `app.state.postgres_main`. `router` is the compiled route
    table, replaced wholesale on each recompile.

    The remaining count failures that no caller is in a position to see, which
    makes them the only record that anything went wrong: `background_errors`
    for background tasks that raised after their response had already been sent,
    `background_timeouts` for tasks cancelled for outrunning `background_timeout`,
    `exception_handler_errors` for a registered error renderer that raised while
    rendering, and `lifespan_teardown_errors` for a resource that refused to
    close while a lifespan failure path was releasing it.

    The three routing backends are behaviourally identical and the tests assert
    parity across them; they differ in how the table compiles. `bitset` and
    `decision` classify a protected route before selecting it, so authentication
    runs once and route selection is filtered by the caller's capabilities; `trie`
    selects first and the same requirement is enforced by the authorization stage.

    Args:
        debug: Put the exception type and message in the body of an unhandled 500.
        routing: Route-table backend -- `bitset` (default), `decision`, or `trie`.
        limits: Body, form and cookie ceilings applied to every request built here.
        background_timeout: Seconds a response's background tasks may run before
            they are cancelled, or `None` for no limit. Default 30.
    """

    __slots__ = (
        "_all_capability_mask",
        "_application_image",
        "_auth_backend",
        "_app_scope",
        "_authorizer",
        "_capabilities",
        "_classify",
        "_compile_lock",
        "_crud_enabled",
        "background_errors",
        "background_timeouts",
        "_background_timeout",
        "exception_handler_errors",
        "lifespan_teardown_errors",
        "_dirty",
        "_dynamic_matcher",
        "_databases",
        "_manage_schema",
        "_exception_handlers",
        "_flight_route_ids",
        "_flight_route_keys",
        "_flight_capture_plan",
        "_flight_arm_registry",
        "_limits",
        "_fallback_exception_handler",
        "_global_after_hooks",
        "_global_before_hooks",
        "_has_global_http_hooks",
        "_global_middleware",
        "_auth_handlers",
        "_handler_requirements",
        "_http_clients",
        "_job_runners",
        "_match",
        "_message_buses",
        "_object_stores",
        "_openapi_security_schemes",
        "_middleware",
        "_middleware_order",
        "_mount_names",
        "_oidc_providers",
        "_orm_registries",
        "_supervisor",
        "_preflight_fallback",
        "_probe",
        "_resolve",
        "_route_methods",
        "_routes",
        "_routing",
        "_shutdown_handlers",
        "_startup_handlers",
        "_stage_hooks",
        "_static_matcher",
        "_validation_formatter",
        "_status_handlers",
        "_webhook_hubs",
        "_websocket_hooks",
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
        routing: RoutingMode = "bitset",
        limits: RequestLimits = DEFAULT_LIMITS,
        background_timeout: float | None = 30.0,
    ) -> None:
        self._routing = routing
        self._limits = limits
        if background_timeout is not None and background_timeout <= 0:
            raise ValueError("background_timeout must be positive, or None for no limit")
        #: Seconds a response's background tasks may run before they are
        #: cancelled. They run *after* the response is sent but still inside the
        #: ASGI invocation, and a conforming server cannot read the next request
        #: on that connection until the invocation returns -- so an unbounded
        #: task is an unbounded stall on a connection whose client has already
        #: been told the work finished. Measured at 1002 ms for a 1 s task over
        #: HTTP/1.1 keep-alive. `None` restores the old unbounded behaviour for
        #: an application that knows what its tasks do.
        self._background_timeout = background_timeout
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
        # Values from `Depends(..., scope="app")`. Owned by this application
        # and torn down by its lifespan shutdown -- never a module global, so
        # two apps in one process do not share dependency instances.
        self._app_scope = AppScope()
        self._validation_formatter: Any = None
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
        self._static_matcher = _StaticMatcher()
        self._dynamic_matcher: _DynamicMatcher | None = None
        self._mount_names: dict[str, str] = {}
        self._startup_handlers: list[LifespanHandler] = []
        self._shutdown_handlers: list[LifespanHandler] = []
        self._routes: list[RouteDefinition] = []
        self._openapi_security_schemes: dict[str, dict[str, Any]] = {}
        #: Every distinct HTTP method any route declares, recomputed on compile.
        #: Read only when a request misses, to tell a 404 from a 405.
        self._route_methods: tuple[str, ...] = ()
        self._application_image = _ApplicationImage(self)
        self._middleware: list[tuple[int, int, Middleware]] = []
        self._global_middleware: list[tuple[int, int, Middleware]] = []
        # Directional request-time programs. Before entries also carry the
        # exact number of after hooks to unwind on failure and short-circuit.
        self._global_before_hooks: tuple[tuple[Any, bool, int, int], ...] = ()
        self._global_after_hooks: tuple[tuple[Any, int], ...] = ()
        self._has_global_http_hooks = False
        # (before_hook, is_sync) for middleware explicitly safe on handshakes.
        self._websocket_hooks: tuple[tuple[Any, bool], ...] = ()
        self._handler_requirements: dict[Any, AuthRequirement] = {}
        # The subset of those whose `needs_backend` is true, so dispatch asks a
        # set rather than a requirement -- see the read site in `__call__`.
        self._auth_handlers: frozenset[Any] = frozenset()
        # Native Flight Recorder route attribution: {compiled_handler:
        # (route_id, plan_id)} joined to the Stage-0 metadata image, built lazily
        # the first time a request carries a live recorder context. None until
        # then, and reset on every route recompile so it never goes stale.
        self._flight_route_ids: dict[Any, tuple[int, int]] | None = None
        self._flight_route_keys: dict[Any, tuple[str, str]] = {}
        # Forensic capture: the compiled redaction plan (the startup ceiling) and
        # the runtime arm registry, both set by the server under a Forensic
        # recorder. None keeps the capture seam a single not-taken branch.
        self._flight_capture_plan: Any = None
        self._flight_arm_registry: Any = None
        self._stage_hooks: dict[str, tuple[Any, ...]] = {}
        self._middleware_order = 0
        self._exception_handlers: dict[type[Exception], ExceptionHandler] = {}
        self._status_handlers: dict[int, ExceptionHandler] = {}
        self._dirty = False
        self._compile_lock = threading.Lock()
        self._crud_enabled = False
        #: Background tasks that raised after their response was sent. Nothing
        #: can be reported to the client at that point, so this is the only
        #: place the failure exists.
        self.background_errors = 0
        self.background_timeouts = 0
        #: Registered exception handlers, status handlers, validation formatters
        #: and fallback handlers that raised while rendering an error response.
        #: The client still gets a 500, so the failure is invisible to it; this
        #: counter and the logged traceback are where it shows up.
        self.exception_handler_errors = 0
        #: Teardown steps -- app-scoped dependencies, object stores, HTTP
        #: clients, databases -- that raised while a lifespan failure path was
        #: releasing them. Teardown continues past a failure so the resources
        #: behind it are still released, and the lifespan reply carries the
        #: original error, so this counter and the logged traceback are the only
        #: record that a close refused.
        self.lifespan_teardown_errors = 0
        self._databases: dict[str, Any] = {}
        #: Whether wreath creates and upgrades its own tables. False for a
        #: deployment whose role cannot CREATE SCHEMA -- the common enterprise
        #: case -- which then gets a startup refusal naming what is missing
        #: rather than a runtime error at the first enqueue.
        self._manage_schema = True
        self._http_clients: dict[str, Any] = {}
        self._orm_registries: dict[str, Any] = {}
        self._oidc_providers: dict[str, Any] = {}
        self._webhook_hubs: dict[str, Any] = {}
        self._job_runners: dict[str, Any] = {}
        self._message_buses: dict[str, Any] = {}
        self._object_stores: dict[str, Any] = {}
        # Built at lifespan startup from the registered runners/buses; owns their
        # process-lifetime worker/consumer/sweeper tasks.
        self._supervisor: Any = None

    def schema_components(self) -> tuple[Any, ...]:
        """Every registered subsystem's claim on the wreath schema.

        Collected by *asking*, not from a list kept in step with the registries:
        anything the application holds that offers `component()` contributes one.
        A hand-maintained list would be one more place to forget a new
        subsystem, and forgetting is exactly the defect this mechanism exists to
        remove -- fourteen subsystems each emitted DDL and nothing ever applied
        any of it, because emitting and applying were never connected.

        Middleware is walked as well as the registries, because the session,
        rate-limit and idempotency stores reach an application that way rather
        than through a registry of their own.

        Returns:
            One `wreath.schema.Component` per registered subsystem that owns
            tables, in registration order, deduplicated by name.
        """
        holders: list[Any] = [
            *self._job_runners.values(),
            *self._message_buses.values(),
            *self._webhook_hubs.values(),
            *self._global_middleware,
            *self._middleware,
        ]
        seen: dict[str, Any] = {}
        for holder in holders:
            for candidate in (holder, *getattr(holder, "schema_owners", ())):
                claim = getattr(candidate, "component", None)
                if claim is None or not callable(claim):
                    continue
                found = claim()
                seen.setdefault(found.name, found)
        return tuple(seen.values())

    def manage_schema(self, manage: bool) -> None:
        """Whether wreath creates and upgrades its own tables. Default True.

        Pass False when the application's database role cannot `CREATE SCHEMA` --
        common in enterprise deployments, and a supported configuration rather
        than an error. Wreath then creates nothing, and instead **refuses at
        startup** if a registered subsystem's tables are absent, naming the
        subsystem, the relation, and the command that emits the DDL for a DBA:

            wreath schema sql --component jobs

        The refusal is at startup rather than at first use deliberately: a
        subsystem that registers is a subsystem that will be used, so checking
        lazily reproduces the exact failure this exists to remove.
        """
        self._manage_schema = bool(manage)
        self._dirty = True

    async def _bootstrap_schema(self) -> None:
        """Bring the wreath schema up to date on every registered database.

        A component names the database it belongs to only indirectly -- through
        the subsystem that declared it -- so the components are grouped by the
        database object each subsystem holds. An application with two databases
        and a job runner on one of them bootstraps that one and leaves the other
        untouched, rather than creating an unused `wreath` schema beside it.
        """
        components = self.schema_components()
        if not components:
            return
        from .schema import bootstrap

        for database, claims in self._components_by_database(components).items():
            await bootstrap(database, claims, manage=self._manage_schema)

    def _components_by_database(self, components: tuple[Any, ...]) -> dict[Any, list[Any]]:
        """Group each claim under the database whose subsystem declared it.

        Some subsystems hold their database; others -- the webhook inbox and
        outbox -- are handed a *session* per call and never see one, so their
        database has to be inferred from the application. Three cases, and the
        middle one is the only judgement call:

        * **No database registered at all**: nothing to bootstrap against, so the
          claim is skipped. That is vacuous rather than degraded -- there is no
          database in which the table could be missing.
        * **Exactly one**: use it. Unambiguous, and the overwhelmingly common
          shape.
        * **More than one**: refuse, naming the component. Guessing would create
          wreath's tables in whichever database happened to be registered first
          and leave the subsystem reading a different one -- a wrong answer
          delivered silently, which is worse than a startup error someone can act
          on.
        """
        owners: dict[int, tuple[Any, list[Any]]] = {}
        for holder, claim in self._schema_owners(components):
            database = getattr(holder, "_database", None) or getattr(holder, "database", None)
            if database is None:
                if not self._databases:
                    continue
                if len(self._databases) > 1:
                    known = ", ".join(sorted(self._databases))
                    raise ValueError(
                        f"cannot tell which database the {claim.name!r} tables belong "
                        f"to: it is handed a session per call rather than a database, "
                        f"and this application registers {len(self._databases)} "
                        f"({known}). Create its tables with `wreath schema sql "
                        f"--component {claim.name}` and set manage_schema(False)."
                    )
                database = next(iter(self._databases.values()))
            owners.setdefault(id(database), (database, []))[1].append(claim)
        return {database: claims for database, claims in owners.values()}

    def _schema_owners(self, components: tuple[Any, ...]) -> list[tuple[Any, Any]]:
        wanted = {claim.name: claim for claim in components}
        pairs: list[tuple[Any, Any]] = []
        holders: list[Any] = [
            *self._job_runners.values(),
            *self._message_buses.values(),
            *self._webhook_hubs.values(),
            *self._global_middleware,
            *self._middleware,
        ]
        for holder in holders:
            for candidate in (holder, *getattr(holder, "schema_owners", ())):
                claim = getattr(candidate, "component", None)
                if claim is None or not callable(claim):
                    continue
                found = claim()
                if wanted.pop(found.name, None) is not None:
                    pairs.append((candidate, found))
        return pairs

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

    def objects(self, name: str, *, backend: str = "local", **options: Any) -> Any:
        """Register a lifespan-managed object-storage backend (`local` or `s3`).

        Exposed on `app.state.objects_<name>`. An `s3` backend owns a pinned
        outbound `HTTPClient` started/stopped with the app; `local` opens its root
        at registration and is closed on shutdown. Credentials come from
        `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` unless
        given explicitly.
        """
        from .objects import LocalObjectStore, S3ObjectStore

        if name in self._object_stores:
            raise ValueError(f"duplicate object store: {name}")
        if backend == "local":
            store: Any = LocalObjectStore(
                options["root"], url_secret=options.get("url_secret")
            )
        elif backend == "s3":
            import os
            from urllib.parse import urlsplit

            from .http_client import HTTPClient

            region = options["region"]
            bucket = options["bucket"]
            scheme = options.get("scheme", "https")
            endpoint = options.get("endpoint")
            path_style = bool(options.get("path_style", bool(endpoint)))
            if endpoint:
                base_url = endpoint if "://" in endpoint else f"{scheme}://{endpoint}"
                host = urlsplit(base_url).hostname or endpoint
                base_url = base_url.rstrip("/")
            else:
                host = f"{bucket}.s3.{region}.amazonaws.com"
                base_url = f"{scheme}://{host}"
            ak = options.get("access_key") or os.environ.get("AWS_ACCESS_KEY_ID")
            sk = options.get("secret_key") or os.environ.get("AWS_SECRET_ACCESS_KEY")
            if not ak or not sk:
                raise ValueError(
                    "s3 object storage needs AWS credentials "
                    "(env AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or access_key=/secret_key=)"
                )
            token = options.get("session_token") or os.environ.get("AWS_SESSION_TOKEN")
            client = HTTPClient(f"objects:{name}", base_url=base_url)
            self._http_clients[f"__objects_{name}"] = client
            store = S3ObjectStore(
                client, bucket=bucket, region=region, access_key=ak, secret_key=sk,
                session_token=token, host=host, scheme=scheme, path_style=path_style,
            )
        else:
            raise ValueError(f"unknown object-store backend: {backend!r}")
        self._object_stores[name] = store
        self.state.__setattr__(f"objects_{name}", store)
        return store

    def oidc_provider(
        self,
        name: str,
        *,
        issuer: str,
        audience: Any = None,
        http_client: Any,
        **options: Any,
    ) -> Any:
        """Register an OIDC identity provider (Cognito/Auth0/Okta/…).

        `http_client` is the name of a client registered with `http_client()` and
        pinned to the issuer origin, or an `HTTPClient` instance. Discovery and the first
        JWKS fetch run during lifespan startup (after HTTP clients start), so
        the first request never pays for them.
        """
        from ._auth.oidc import OidcProvider

        if name in self._oidc_providers:
            raise ValueError(f"duplicate OIDC provider: {name}")
        client = self._http_clients[http_client] if isinstance(http_client, str) else http_client
        provider = OidcProvider(
            name, issuer=issuer, audience=audience, http_client=client, **options
        )
        self._oidc_providers[name] = provider
        self.state.__setattr__(f"oidc_{name}", provider)

        async def _discover(_app: Any) -> None:
            await provider.discover()

        self._startup_handlers.append(_discover)
        self._dirty = True
        return provider

    def oauth2_login(
        self,
        name: str,
        *,
        provider: Any,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: tuple[str, ...] = ("openid", "email"),
        **options: Any,
    ) -> None:
        """Register `/auth/login` + `/auth/callback` for `provider`.

        Env/auth gating is the app's business; this only wires the routes. See
        the design's SSO bridge: the callback verifies the id_token with the
        provider's own JWKS verifier and writes a principal into the session.
        """
        from ._auth.oauth2 import register_oauth2_login

        resolved = self._oidc_providers[provider] if isinstance(provider, str) else provider
        register_oauth2_login(
            self,
            name,
            provider=resolved,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            **options,
        )

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

    def jobs(
        self,
        name: str,
        *,
        database: str,
        workload: str = "write",
        concurrency: int = 8,
        lease: float = 30.0,
        poll_interval: float = 5.0,
        schema: str = "wreath",
        batch: int = 1,
        progress: Any = None,
    ) -> Any:
        """Configure a durable job runner on an existing `app.postgres()` database.

        Its workers, sweeper, and scheduler run for the process lifetime, started
        during lifespan after the databases come up. See `wreath.jobs`.

        Pass a `ProgressRegistry` to make the queue watchable: `JobRunner.launch()`
        hands back a task id, a handler reports through `ctx.report()`, and the
        runner sets the terminal state itself. Give the registry the message bus and
        a job running on any worker is watchable from every worker.
        """
        from .jobs import JobRunner

        if name in self._job_runners:
            raise ValueError(f"duplicate job runner: {name}")
        if database not in self._databases:
            known = ", ".join(sorted(self._databases)) or "none"
            raise KeyError(f"unknown database {database!r}; configured: {known}")
        runner = JobRunner(
            self._databases[database], name=name, workload=workload,
            concurrency=concurrency, lease=lease, poll_interval=poll_interval,
            schema=schema, batch=batch, progress=progress,
        )
        self._job_runners[name] = runner
        self.state.__setattr__(f"jobs_{name}", runner)
        self._dirty = True
        return runner

    def messaging(
        self,
        name: str,
        *,
        database: str,
        workload: str = "write",
        schema: str = "wreath",
        poll_interval: float = 5.0,
        lease: float = 30.0,
    ) -> Any:
        """Configure a pub/sub + durable message bus on an `app.postgres()`
        database. Consumers run for the process lifetime. See `wreath.messaging`."""
        from .messaging import MessageBus

        if name in self._message_buses:
            raise ValueError(f"duplicate message bus: {name}")
        if database not in self._databases:
            known = ", ".join(sorted(self._databases)) or "none"
            raise KeyError(f"unknown database {database!r}; configured: {known}")
        bus = MessageBus(
            self._databases[database], name=name, workload=workload, schema=schema,
            poll_interval=poll_interval, lease=lease,
        )
        self._message_buses[name] = bus
        self.state.__setattr__(f"messaging_{name}", bus)
        self._dirty = True
        return bus

    def orm(
        self,
        *,
        database: str,
        models: Iterable[type],
        validate_schema: Literal["off", "warn", "error"] = "error",
        query_cache_size: int = 512,
        schema_mode: Any = None,
    ) -> Any:
        """Compile `models` against an existing `app.postgres()` database.

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
            schema_mode=schema_mode,
        )
        self._orm_registries[database] = registry
        self.state.__setattr__(f"orm_{database}", registry)
        self._dirty = True
        return registry

    def flags(self, provider: Any = None, **values: str) -> Any:
        """Register a feature-flag provider on `app.state.flags`, and return it.

        Pass a `FlagProvider` (e.g. `FeatureFlags(...)`), keyword flag values, or
        nothing to build one from `WREATH_FLAG_<NAME>` in the environment.

        The provider is returned because that is how it reaches a handler.
        `wreath.flags.flags_dependency` captures the provider it is given once,
        when the dependency is built, and never reads `app.state`. Wire it as
        `Depends(flags_dependency(app.flags(...)))`.
        """
        from .flags import FeatureFlags

        if provider is None:
            provider = FeatureFlags(values) if values else FeatureFlags.from_env()
        self.state.__setattr__("flags", provider)
        return provider

    def health(
        self,
        *,
        checks: Iterable[Any] = (),
        liveness_path: str = "/health",
        readiness_path: str = "/ready",
        is_live: Any = None,
    ) -> Any:
        """Mount liveness and readiness endpoints, `/health` and `/ready` by default.

        `checks` are readiness probes: one critical failure answers 503 on the
        readiness path, and a load balancer takes the instance out of rotation.
        `is_live` decides liveness alone -- returning false answers 503 while the
        process drains -- and reports the process, never its dependencies.

        This mounts that pair only. `wreath.health.health_router` also builds an
        alerts endpoint, for conditions that need a person rather than a load
        balancer, and there is no argument for it here: build that router yourself
        and pass it to `include_router()`.
        """
        from .health import health_router

        router = health_router(
            checks, liveness_path=liveness_path, readiness_path=readiness_path, is_live=is_live
        )
        self.include_router(router)
        return router

    def metrics(
        self, source: Any, *, path: str = "/metrics", namespace: str = "wreath",
        route_labels: Any = None,
    ) -> Any:
        """Mount a Prometheus scrape endpoint rendering `source.snapshot()`."""
        from ._prometheus import metrics_router

        router = metrics_router(source, path=path, namespace=namespace, route_labels=route_labels)
        self.include_router(router)
        return router

    def users(self, store: Any, *, secret: str, **options: Any) -> Any:
        """Mount the user-management lifecycle router (register/login/verify/reset).

        Mounted under `/users` unless `options` says otherwise; `options` are
        `wreath.users.user_router`'s keyword arguments. `secret` signs the
        verification and password-reset tokens, so it must be stable across
        restarts. Login writes the session principal the auth stack reads, which
        requires `SessionMiddleware`: without it `/login` answers 500 and signs
        nobody in.
        """
        from .users import user_router

        router = user_router(store, secret=secret, **options)
        self.include_router(router)
        return router

    def enable_crud(self) -> None:
        """Allow `crud()` to mount auto-generated CRUD routers.

        CRUD is off by default: generating write endpoints from a model is a
        deliberate, app-wide decision, so it must be turned on explicitly before
        any model can opt in.
        """
        self._crud_enabled = True

    def crud(self, model: type, open_session: Any, **options: Any) -> Any:
        """Mount auto-generated CRUD routes for one `model` (requires opt-in).

        Off unless `enable_crud()` was called (config-level opt-in) *and* you
        call this per model (model-level opt-in); without the first this raises
        `RuntimeError`. A column whose name looks sensitive is hidden from
        responses unless named in `expose=`, and it stays unwritable even then --
        no generated route will ever set one. `options` are `crud_router()`'s
        keyword arguments. See `wreath.crud`.
        """
        if not getattr(self, "_crud_enabled", False):
            raise RuntimeError(
                "CRUD is disabled by default; call app.enable_crud() before app.crud(...)"
            )
        from .crud import crud_router

        router = crud_router(model, open_session, **options)
        self.include_router(router)
        return router

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
        include_in_schema: bool = True,
        security: Mapping[str, Iterable[str]] | None = None,
        name: str | None = None,
        host: str | None = None,
    ) -> Callable[[Handler], Handler]:
        """Register the decorated handler for `path` under each of `methods`.

        The handler is returned unchanged, so it stays directly callable and stacks
        with `@authenticated`, `@roles`, `@permissions` and `@authorize`, whose
        requirements are merged with `permissions=` rather than replaced by it.

        A handler's first parameter is the `Request`. Every further parameter is
        bound by name at compile time from the path, query string, headers, cookies
        or body. Where the source is not obvious, name it with a marker inside
        `Annotated` and leave the default an ordinary Python default --
        `limit: Annotated[int, Query(...)] = 20`, never `Query(20)`.

        A handler may return a `Response`, `StreamingResponse`, `FileResponse` or
        `PreparedResponse` to control the whole reply, or a value that is coerced
        into one: `str` becomes `text/plain; charset=utf-8`, `bytes` becomes
        `application/octet-stream`, and a `dict`, `list`, `tuple`, number, `bool` or
        `None` becomes `application/json`. Any other type is a `TypeError`, which
        the dispatch error boundary turns into a 500 like any other handler error.

        Set `response_only=True` only when the handler always returns a response
        object. On a route with middleware this removes the coercing wrapper
        between the handler and the middleware; violating the promise may hand
        a non-response value to an egress hook.

        Registration only appends to the route table. Signature inspection, binder
        compilation, middleware fusing and capability-mask assignment all happen
        once, when the routes are compiled, and cost a request nothing.

        Args:
            methods: HTTP methods, upper-cased here. A GET route also answers HEAD.
            middleware: Route-scoped middleware, applied inside app-wide middleware.
            tags: OpenAPI tags for the operation.
            summary: OpenAPI summary. The handler's docstring becomes its description.
            dependencies: `Depends` values resolved per request before the handler.
            permissions: Permission names the caller must hold, all of them.
            operation_id: OpenAPI operationId, and the generated clients' method name.
            response_only: Handler promises to return a response object directly.

        Raises:
            ValueError: This method and path are already registered.
        """
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
        if not 100 <= status_code <= 599:
            raise ValueError("status_code must be between 100 and 599")
        response_specs = tuple((int(code), spec) for code, spec in (responses or {}).items())
        route_security = tuple(
            (name, tuple(scopes)) for name, scopes in (security or {}).items()
        )

        def register(handler: Handler) -> Handler:
            check_placeholders(path)
            if name is not None and any(route.name == name for route in self._routes):
                raise ValueError(f"route name {name!r} is already registered")
            dynamic = host is not None or ":path}" in path
            for method in route_methods:
                if dynamic:
                    if any(
                        method in route.methods and route.path == path and route.host == host
                        for route in self._routes
                    ):
                        raise ValueError(
                            f"route {method} {path!r} for host {host!r} is already registered"
                        )
                else:
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
                    response_only,
                    status_code,
                    response_description,
                    response_media_type,
                    response_specs,
                    deprecated,
                    include_in_schema,
                    route_security,
                    name,
                    host,
                )
            )
            self._dirty = True
            return handler

        return register

    def get(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a GET route. A GET route answers HEAD as well, without a body.

        `metadata` is `route()`'s keyword arguments -- `middleware`, `tags`,
        `summary`, `dependencies`, `permissions`, `operation_id` -- and the handler
        contract, parameter binding and return-value coercion are the same.
        """
        return self.route(path, methods=("GET",), **metadata)

    def post(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a POST route. `metadata` is `route()`'s keyword arguments."""
        return self.route(path, methods=("POST",), **metadata)

    def put(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a PUT route. `metadata` is `route()`'s keyword arguments."""
        return self.route(path, methods=("PUT",), **metadata)

    def patch(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a PATCH route. `metadata` is `route()`'s keyword arguments."""
        return self.route(path, methods=("PATCH",), **metadata)

    def delete(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a DELETE route. `metadata` is `route()`'s keyword arguments."""
        return self.route(path, methods=("DELETE",), **metadata)

    def _named_route(self, name: str) -> RouteDefinition:
        for definition in self._routes:
            if definition.name == name:
                return definition
        raise KeyError(f"no route named {name!r}")

    def url_path_for(self, name: str, **parameters: Any) -> str:
        """Build a percent-encoded path for a named route or mount."""
        mount = self._mount_names.get(name)
        if mount is not None:
            suffix = parameters.get("path", "")
            if suffix:
                return mount.rstrip("/") + "/" + quote(str(suffix).lstrip("/"), safe="/")
            return mount
        return _render_route_template(self._named_route(name).path, parameters)

    def _host_for(self, name: str, parameters: Mapping[str, Any]) -> str | None:
        if name in self._mount_names:
            return None
        host = self._named_route(name).host
        return _render_route_template(host, parameters) if host is not None else None

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
        """Copy `router`'s routes onto this application beneath `prefix`.

        The router is read here, not retained: each of its routes is re-registered
        on the application with the prefix applied, and this call's `tags`,
        `middleware` and `dependencies` placed ahead of the route's own. Routes
        added to `router` afterwards are not picked up -- include it last.

        Access requirements merge rather than replace. A route keeps whatever
        `@authenticated`, `@roles`, `@permissions` or `@authorize` it declares, and
        `permissions=` here is required on top of it, so including a router can only
        tighten a route.

        Args:
            prefix: Path prefix. Must begin with `/`; a trailing `/` is stripped.
            permissions: Permission names required on every included route, all of them.

        Raises:
            ValueError: `prefix` does not begin with `/`, or a route it adds is a duplicate.
        """
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
            if definition.name is not None and any(
                route.name == definition.name for route in self._routes
            ):
                raise ValueError(f"route name {definition.name!r} is already registered")
            dynamic = definition.host is not None or ":path}" in path
            for method in definition.methods:
                if not dynamic:
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
                    definition.response_only,
                    definition.status_code,
                    definition.response_description,
                    definition.response_media_type,
                    definition.responses,
                    definition.deprecated,
                    definition.include_in_schema,
                    definition.security,
                    definition.name,
                    definition.host,
                )
            )
        self._dirty = True

    def configure_auth(
        self,
        backend: AuthenticationBackend,
        authorizer: AuthorizationProvider | None = None,
    ) -> None:
        """Install the application-wide authentication backend and policy authorizer.

        `backend.authenticate(request)` returns an `Identity` or `None` and runs at
        most once per request: before route selection when the compiled table
        classified the path as protected, otherwise on first need in the
        authorization stage. A request that produces no identity where one is
        required becomes a 401 carrying `backend.challenge(request)` as its
        `WWW-Authenticate` value; an identity that fails a role or permission check
        becomes a 403.

        `authorizer` is consulted only for routes carrying `@authorize` policies.
        Such a route with no authorizer configured raises `RuntimeError` when it is
        requested, rather than allowing the request through.

        Calling this replaces any previous backend and authorizer, and marks the
        routes for recompilation.
        """
        self._auth_backend = backend
        self._authorizer = authorizer
        self._dirty = True

    def add_middleware(self, middleware: Middleware, *, priority: int = 0) -> None:
        """Register middleware, routed by its own scope to one of two pipelines.

        Middleware carrying a truthy `global_scope` attribute -- `PipelineHooks`, and
        the shipped CORS, CSRF, compression, rate-limit, request-id, security,
        timing and idempotency middlewares -- goes to `add_global_middleware()` and
        wraps the whole request, routing misses and static files included. Anything
        else is route middleware: it is compiled into each route's tape and runs
        only once a route has matched and its authorization has passed, so it never
        sees a 404 or a 401.

        Route middleware is either the hook form (`before`, `before_sync`, `after`,
        or a `MiddlewareHooks`) or the legacy `(request, call_next)` callable. An
        object carrying both raises `TypeError` when the routes compile, rather than
        silently running one and ignoring the other.

        Ordering within a pipeline is by `priority` ascending, ties broken by
        registration order, and the first item is the outermost: its `before` runs
        first and its `after` runs last. Middleware passed to `route(middleware=...)`
        sits inside everything registered here.

        The `after` guarantee is narrower than it is usually stated, and it differs
        between the two pipelines. Global: an `after` hook runs only if its own
        `before` completed, where returning a response counts as completing, so it
        may run for a request that never reached the endpoint and may be handed an
        error response rather than the handler's; an `after` that raises becomes an
        error response and the remaining `after` hooks keep unwinding with it.
        Route: a `before` that returns a response still runs its own `after` and
        those outside it, but a `before` or an `after` that *raises* abandons the
        rest of that tape entirely -- no further `after` hook on the route runs, and
        the exception becomes the response through the ordinary error boundary.

        Args:
            priority: Lower runs earlier and further out. Ties keep registration order.
        """
        if getattr(middleware, "global_scope", False):
            self.add_global_middleware(middleware, priority=priority)
            return
        self._middleware.append((priority, self._middleware_order, middleware))
        self._middleware_order += 1
        self._dirty = True

    def add_global_middleware(self, middleware: Middleware, *, priority: int = 0) -> None:
        """Register hook middleware around routing and all HTTP responses.

        Global middleware must expose `before`, `before_sync`, and/or
        `after` hooks. Unlike route middleware, it covers misses, static
        files, and authorization failures, so it is suitable for ingress checks
        and response headers.

        A `PipelineHooks` may also carry the stage hooks `miss`, `pre_auth`,
        `identity` and `action`, each running at the pipeline boundary it names and
        each able to end the request by returning a response. Ordering and the
        `after` contract are those documented on `add_middleware()`.

        A middleware may additionally expose `handle_preflight`, which answers an
        OPTIONS request that matched no route. Only one may, because two CORS
        policies answering the same preflight would silently resolve to whichever
        was registered first.

        Raises:
            TypeError: `middleware` exposes none of the three hooks.
            ValueError: A second `handle_preflight` middleware was registered.
        """
        if not any(
            hasattr(middleware, name)
            for name in (
                "before", "before_sync", "before_websocket", "after", "after_sync",
                "after_inplace",
            )
        ):
            raise TypeError(
                "global middleware must expose before, before_sync, "
                "before_websocket, after, after_sync, and/or after_inplace hooks"
            )
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
        """Register middleware as a decorator or a direct call.

        `@app.middleware` registers the object it decorates and hands it back
        unchanged; `@app.middleware(priority=10)` returns the decorator that does
        the same; `app.middleware(obj)` registers `obj` directly. Every form goes
        through `add_middleware()`, so a `global_scope` object still lands in the
        global pipeline and the ordering rules there apply unchanged.
        """

        def register(value: Middleware) -> Middleware:
            self.add_middleware(value, priority=priority)
            return value

        return register if middleware is None else register(middleware)

    def mount(self, prefix: str, application: Any, *, name: str | None = None) -> None:
        """Mount an arbitrary ASGI application below `prefix`.

        The child receives a path with the mount prefix removed and a
        `root_path` extended by that prefix. Parent global middleware still
        runs and its response-header edits are merged into the child's response;
        the child's own status, streaming, routing and middleware remain
        independent.
        """
        if not prefix.startswith("/"):
            raise ValueError("mount prefixes must begin with '/'")
        mount_root = "/" + prefix.strip("/")
        normalized = mount_root + "/"
        if name is not None:
            if name in self._mount_names or any(route.name == name for route in self._routes):
                raise ValueError(f"route name {name!r} is already registered")
            self._mount_names[name] = mount_root

        async def mounted(request: Request) -> _MountedResponse:
            child_scope = dict(request.scope)
            remainder = request.path_params.get("path", "")
            child_scope["path"] = "/" + remainder.lstrip("/")
            child_scope["raw_path"] = child_scope["path"].encode("utf-8")
            parent_root = str(child_scope.get("root_path", "")).rstrip("/")
            child_scope["root_path"] = parent_root + mount_root
            return _MountedResponse(application, child_scope, request._receive)

        self._static_matcher.add(normalized, cast("Handler", mounted))

    def static(
        self,
        prefix: str,
        directory: str,
        *,
        html_index: bool = True,
        cache_control: CacheControl | None = None,
    ) -> None:
        """Serve files under `directory` for paths beginning with `prefix`.

        Mounts are consulted only when no route matches, so they cost nothing
        on the routed hot path.

        `prefix` is normalised to a leading and trailing `/`. Mounts are scanned in
        registration order and the first whose prefix matches wins, so precedence is
        registration order and not longest prefix: mounting `/assets/` before
        `/assets/images/` leaves the second unreachable. Register the narrower mount
        first. A prefix registered twice keeps its first handler.

        Args:
            html_index: Serve `index.html` for a directory path; when False it 404s.
            cache_control: Sent as Cache-Control on every file from this mount.

        Raises:
            ValueError: `directory` does not exist or is not a directory.
        """
        from .staticfiles import StaticFiles

        normalized = "/" + prefix.strip("/") + "/"
        handler = StaticFiles(
            directory, html_index=html_index, cache_control=cache_control
        )
        self._static_matcher.add(normalized, cast("Handler", handler))

    def websocket(self, path: str) -> Callable[[WebSocketHandler], WebSocketHandler]:
        """Register a WebSocket handler; it receives one WebSocket per connection.

        WebSocket routes live in their own table, so a path may carry both an HTTP
        route and a WebSocket route without either shadowing the other.

        A handler carrying `@authenticated`, `@roles`, `@permissions` or
        `@authorize` has it enforced before the handshake is accepted, using the
        same backend `configure_auth()` installed and reading the headers and
        cookies the handshake carries. A refused caller is closed with 1008 and
        never holds an accepted socket; a path with no WebSocket route is closed
        with 1000. Servers turn a pre-accept close into their own rejection.

        A `WebSocketDisconnect` raised out of the handler is absorbed: the peer has
        already gone and there is nothing left to send.
        """

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
        """Handle `error_type` and its subclasses with `handler`.

        `handler(request, error)` is awaited and may return anything a route handler
        may return; the result is coerced the same way, and a bare value therefore
        gets status 200 -- return a `Response` or `ProblemResponse` to choose the
        status. Lookup walks the raised exception's MRO and takes the first
        registration it finds, so the most derived registered class wins and a
        handler registered for `Exception` catches `HTTPException` too. Registering
        one for `NotFound` also covers a routing miss.

        Registered handlers take precedence over everything built in. With none
        registered, an `HTTPException` becomes an RFC 9457
        `application/problem+json` body carrying its status, title and detail, a
        `ValidationError` becomes a 422 whose `errors` extension holds the field
        errors, and anything else becomes a 500 whose detail is `Internal Server
        Error` -- never a `{"detail": ...}` body. `BaseException` is caught nowhere
        in dispatch, so `CancelledError`, `KeyboardInterrupt` and `SystemExit` still
        unwind the request.

        A handler that itself raises does not end the request without a reply.
        The failure is counted on `exception_handler_errors` and logged with its
        traceback to the `wreath` logger, and the client is answered with the
        built-in 500 problem document -- in `debug`, one naming the handler's own
        exception rather than the original. Treat a non-zero count as a bug in
        the handler: it means clients are getting a generic 500 where the
        application meant to shape something.
        """
        self._exception_handlers[error_type] = handler

    def exception_handler(
        self, error_type: type[Exception]
    ) -> Callable[[ExceptionHandler], ExceptionHandler]:
        """Register the decorated function with `add_exception_handler()`.

        The function is returned unchanged, and the handler contract, precedence
        and coercion rules are exactly that method's.
        """

        def register(handler: ExceptionHandler) -> ExceptionHandler:
            self.add_exception_handler(error_type, handler)
            return handler

        return register

    def add_status_handler(self, status: int, handler: ExceptionHandler) -> None:
        """Render every `HTTPException` of this status with `handler`.

        `handler(request, error)` receives the `HTTPException` itself and is awaited;
        its return value is coerced exactly as a route handler's, so a plain `dict`
        or `str` is sent with status 200 -- the handler owns the status, and should
        return a `Response` or `ProblemResponse` carrying `status`.

        Status handlers are consulted only after the exception handlers, and only
        for an `HTTPException`. A `ValidationError` is not one: shape a 422 with
        `set_validation_formatter()`, or with an exception handler registered for
        `ValidationError`. An ordinary crash is not one either, so a handler
        registered for 500 sees a deliberately raised `HTTPException` of that
        status and never an unhandled error.
        """
        self._status_handlers[status] = handler

    def set_validation_formatter(self, formatter: Any) -> None:
        """Shape 422 bodies with `formatter(errors, request) -> ProblemDetail`.

        `errors` is the raw list of `{"loc", "msg", "type"}` dicts from the
        validator. Pass `None` to restore the built-in RFC 9457 output.
        `wreath.validation_errors` ships a catalogue-backed formatter that
        translates on `type` and negotiates `Accept-Language`.
        """
        self._validation_formatter = formatter

    def add_security_scheme(self, name: str, schema: Mapping[str, Any]) -> None:
        """Declare one OpenAPI security scheme used by route metadata.

        The schema is copied at registration. Routes opt into it with
        `security={name: scopes}`; naming an undeclared scheme is refused by
        OpenAPI generation rather than emitting a dangling reference.
        """
        if not name:
            raise ValueError("security scheme name must not be empty")
        if "type" not in schema:
            raise ValueError("security scheme requires a type")
        self._openapi_security_schemes[name] = dict(schema)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Send) -> None:
        """Serve one ASGI connection. The application's entry point on any server.

        Handles the three scope types wreath implements -- `http`, `websocket` and
        `lifespan` -- and raises `ValueError` naming the type for anything else,
        rather than returning silently, because an ASGI server that offers a scope
        this application cannot serve is a deployment error and not a request error.

        Compilation happens here, before dispatch, whenever registration has marked
        the application dirty. That covers the first request and any registration
        made after startup, and it is guarded by a lock, so under free-threading two
        threads arriving together compile once.
        """
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
        before_hooks = self._global_before_hooks
        after_hooks = self._global_after_hooks
        global_hooks = self._has_global_http_hooks
        active_global = len(after_hooks)
        if global_hooks:
            request = Request(scope, receive, limits=self._limits, app=self)
            request._route_outcome = "ingress"
            for before, is_sync, error_afters, success_afters in before_hooks:
                try:
                    candidate = before(request) if is_sync else await before(request)
                except Exception as error:  # noqa: BLE001 -- see _handle_exception
                    # `error_afters` excludes this hook's own egress: its
                    # `before` did not complete, so cleanup preconditions may
                    # not exist. Earlier hooks did complete and still unwind.
                    await self._finish_http(
                        request,
                        _coerce_response(await self._handle_exception(request, error)),
                        send,
                        method,
                        scope,
                        native_response,
                        error_afters,
                    )
                    return
                if candidate is not None:
                    # Returning a response is a *completed* `before`, so this
                    # hook keeps its egress, which `success_afters` includes.
                    active_global = success_afters
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

        if not native_response and _ambiguous_request_path(scope, path):
            if request is None:
                request = Request(scope, receive, limits=self._limits, app=self)
            response = await self._handle_exception(
                request,
                BadRequest("ambiguous encoded path separator or dot segment"),
            )
            await self._finish_http(
                request, response, send, method, scope, native_response, active_global
            )
            return

        matched = None
        dynamic_matcher = self._dynamic_matcher
        if dynamic_matcher is not None:
            if request is None:
                request = Request(scope, receive, limits=self._limits, app=self)
            matched = dynamic_matcher.match(
                method,
                path,
                _host_name(request.header("host", "") or ""),
                host_routes=True,
            )
        if matched is None and self._routing in _CLASSIFYING:
            classify = self._classify
            resolve = self._resolve
            if classify is None or resolve is None:
                raise RuntimeError("classifying router did not expose classify/resolve")
            classification, payload = classify(method, path)
            matched = payload if classification == 1 else None
            if classification == 2:
                ticket = payload
                if request is None:
                    request = Request(scope, receive, limits=self._limits, app=self)
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
                try:
                    identity = await backend.authenticate(request)
                except Exception as error:  # noqa: BLE001 -- see _handle_exception
                    # On a classifying table authentication runs *here*, before
                    # the route is resolved, and this is the error boundary it
                    # was missing. Without it an exception from a backend left
                    # `__call__` with no response started at all: the ASGI
                    # server, not wreath, decided what the caller saw,
                    # `_handle_exception` never ran, and every global `after`
                    # hook was skipped -- so the security headers, the access
                    # log and the recorder's finish went missing for exactly the
                    # requests that failed inside authentication. The `trie`
                    # path answered 500 for the same backend through
                    # `_authorize_request`, which made the difference a routing
                    # mode nobody connected to error handling.
                    #
                    # A backend is documented to refuse with `None` rather than
                    # raise, so this is misuse -- but it is also reachable
                    # without any application mistake: `OidcProvider`'s verifier
                    # awaits a JWKS fetch in here, so an identity provider that
                    # is merely unreachable raises straight through.
                    if global_hooks:
                        request._set_route_outcome("protected")
                    response = await self._handle_exception(request, error)
                    await self._finish_http(
                        request, response, send, method, scope, native_response, active_global
                    )
                    return
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
                        try:
                            error = Unauthorized(challenge=backend.challenge(request))
                        except Exception as raised:  # noqa: BLE001 -- as above
                            # The other half of the same boundary. `challenge`
                            # is backend code too, and it is only ever called on
                            # a path that is already refusing, so a raise here
                            # replaced a 401 with an escaped exception.
                            # `_authorize_request` calls it inside its caller's
                            # try; this call had nothing around it.
                            if global_hooks:
                                request._set_route_outcome("protected")
                            response = await self._handle_exception(request, raised)
                            await self._finish_http(
                                request, response, send, method, scope,
                                native_response, active_global,
                            )
                            return
                    else:
                        error = Forbidden("Forbidden")
                    if global_hooks:
                        request._set_route_outcome("protected")
                    response = await self._handle_exception(request, error)
                    await self._finish_http(
                        request, response, send, method, scope, native_response, active_global
                    )
                    return
        elif matched is None:
            # Trie routes are selected without capability filtering; the common
            # authorization stage below checks the compiled route requirement.
            matched = self._route_match(method, path, 0)
        if matched is None and dynamic_matcher is not None:
            if request is None:
                request = Request(scope, receive, limits=self._limits, app=self)
            matched = dynamic_matcher.match(
                method,
                path,
                _host_name(request.header("host", "") or ""),
                host_routes=False,
            )
        if matched is None:
            if request is None:
                request = Request(scope, receive, limits=self._limits, app=self)
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
            static_match = self._static_matcher.match(path)
            if static_match is not None:
                matched = static_match
                if global_hooks:
                    request._set_route_outcome("static")
            else:
                # The path may exist under another method. Answering that 404
                # told a client to stop looking when the correct reply is "not
                # like that": RFC 9110 15.5.6 is a 405 carrying `Allow`. The
                # probe re-runs classification once per registered method, which
                # is a handful of lookups on a path that is already an error --
                # nothing on the hot path pays for it.
                allow = self._allowed_methods(
                    method, path, _host_name(request.header("host", "") or "")
                )
                # Through the exception path rather than straight to a
                # ProblemResponse, so a registered 404 status handler (or a
                # NotFound exception handler) covers a routing miss -- which is
                # the case people register one for. With nothing registered this
                # produces the same response it always did.
                if allow:
                    response = await self._handle_exception(
                        request, MethodNotAllowed(allow=allow)
                    )
                else:
                    response = (
                        _NOT_FOUND
                        if not self._exception_handlers and not self._status_handlers
                        else await self._handle_exception(request, NotFound("Not Found"))
                    )
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
                # One log scope per request, keyed on the recorder's own request
                # id so a record joins the completion the projector will
                # assemble. The context goes in rather than the id: reading the
                # id crosses into C, and `begin_request_for` checks for an
                # installed runtime first, so a recorder without logging pays a
                # call and a branch and no crossing. `_finish_http` closes the
                # scope -- every exit from this method funnels through there,
                # error paths included.
                _log_begin_request(scope)
                ids = self._flight_route_ids or self._build_flight_route_ids()
                attribution = ids.get(handler)
                if attribution is not None:
                    scope._flight_stamp(attribution[0], attribution[1])
                if flight == 2:
                    # Armed for Detailed capture: bind the phase marker once so
                    # the handler/serialize timing below is a plain local call,
                    # and propagate it so dependency seams (PostgreSQL, HTTP
                    # client) can record their phases from inside the handler.
                    # Set-without-reset is safe: the request task's context dies
                    # with it, and the protocol severs the context at completion
                    # so an escaped binding no-ops instead of writing stale.
                    flight_phase = scope._flight_phase
                    _phase_marker.set(flight_phase)
        elif "_wreath_flight" in scope:
            # HTTP/2 and HTTP/3 dispatch through a dict scope with no request
            # context, so their protocols seed the recorder's request id into
            # `_wreath_flight`. The seeded id opens this request's log scope
            # before the line below overwrites it with route attribution; the
            # helper reads the key itself, so a server without a logging runtime
            # pays a Python call and no crossing.
            _log_begin_seeded(scope)
            ids = self._flight_route_ids or self._build_flight_route_ids()
            attribution = ids.get(handler)
            if attribution is not None:
                scope["_wreath_flight"] = attribution
        if request is None:
            request = Request(scope, receive, path_params, self._limits, app=self)
        else:
            request.path_params = path_params or {}
        # Forensic capture (native armed path only): flight_phase is non-None
        # exactly when flight == 2, and the plan is set only under a Forensic
        # recorder, so this is one predicted branch for every other request.
        # Request-header capture (and the per-arm match count) happens here and
        # returns the active-arm snapshot; the response headers and request/
        # response bodies are captured at completion under that same snapshot,
        # and any DB/outbound seam consults the bound dependency capturer.
        flight_arms = (
            self._capture_request(scope, request)
            if flight_phase is not None and self._flight_capture_plan is not None
            else None
        )
        if flight_arms is not None:
            dep_rule = _narrow_dependency(flight_arms)
            if dep_rule is not None:
                _capture_marker.set(_make_dependency_capturer(scope, dep_rule))
        if global_hooks and request._get_route_outcome() in (None, "ingress"):
            request._set_route_outcome("route")
        # `_auth_handlers` is `needs_backend` decided once, at compile time: the
        # answer cannot change between requests, and asking a requirement per
        # request costs a property call and several attribute reads on the path
        # every public route takes. `_handler_requirements` stays complete --
        # `wreath-request-trace` reads it to learn which compiled endpoints are
        # route code.
        requirement = (
            self._handler_requirements.get(handler)
            if handler in self._auth_handlers
            else None
        )
        if requirement is not None:
            try:
                if flight_phase is None:
                    stage_response = await self._authorize_request(request, requirement)
                else:
                    auth_start = _monotonic_ns()
                    stage_response = await self._authorize_request(request, requirement)
                    flight_phase(
                        _PH_AUTH, 0, _COV_PYTHON, _monotonic_ns() - auth_start
                    )
            except Exception as error:  # noqa: BLE001 -- see _handle_exception
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
        except Exception as error:  # noqa: BLE001 -- see _handle_exception
            response = await self._handle_exception(request, error)

        # Forensic response-side capture: the response now exists (handler result,
        # coerced response, or error response). Only fires when request-header
        # capture found active arms, so it rides the same match already counted.
        if flight_arms is not None:
            self._capture_completion(scope, request, response, flight_arms)

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
        hooks = self._global_after_hooks
        # Counted down by index rather than `reversed(hooks[:active_global])`,
        # which allocated a fresh list slice on every response.
        index = active_global
        while index:
            index -= 1
            after, mode = hooks[index]
            try:
                if mode == 2:
                    after(request, response)
                else:
                    candidate = (
                        after(request, response)
                        if mode == 1
                        else await after(request, response)
                    )
                    response = _coerce_response(candidate)
            except Exception as error:  # noqa: BLE001 -- see _handle_exception
                response = await self._handle_exception(request, error)

        # Close this request's log scope before the response goes out, so its
        # records are published while the recorder still holds the context they
        # will be joined to. The response goes in rather than a verdict: reading
        # a native response's status is a crossing, and `finish_request_for`
        # checks for a bound scope first, so a request with no logging runtime
        # pays one `ContextVar.get(None)` and no crossing.
        _log_finish_request(response)

        extensions = None if native_response else scope.get("extensions")
        if method == "HEAD":
            await response(_head_send(send))
        elif type(response).__call__ is _RESPONSE_CALL and native_response:
            plain = cast(Response, response)
            protocol = cast(Any, send).__self__
            await protocol._wreath_response(plain.status, plain.headers, plain.body)
        elif type(response).__call__ is _RESPONSE_CALL and (
            extensions is not None and "wreath.response" in extensions
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
            try:
                # The deadline is on the *group*, not each task: they run in
                # sequence and it is the total that holds the connection open.
                async with _asyncio_timeout(self._background_timeout):
                    await background()
            except TimeoutError:
                # The tasks have already been cancelled by the timeout. Counted
                # apart from `background_errors` because nothing failed -- work
                # was stopped, and the two want different responses: a rising
                # error count is a bug, a rising timeout count is work that does
                # not belong after a response.
                self.background_timeouts += 1
                raise
            except Exception:
                # Counted, then re-raised. Propagating is the shipped contract
                # (tests/test_background.py asserts it) and it is the right one:
                # the response has already gone, so the *server* is the only
                # thing left that can log this, and swallowing it here would
                # take that away. What was missing is that the application had
                # no way to know it had happened at all.
                self.background_errors += 1
                raise

    async def _handle_websocket(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if _ambiguous_request_path(scope, scope["path"]):
            await send({"type": "websocket.close", "code": 1008})
            return
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
            # A WebSocket session is one recorder context for its whole life, so
            # its log scope spans the session too: records made while the socket
            # is open belong to it, and a session that ends badly promotes the
            # verbose ones exactly as a failed request does.
            _log_begin_seeded(scope)
            ids = self._flight_route_ids or self._build_flight_route_ids()
            attribution = ids.get(handler)
            if attribution is not None:
                scope["_wreath_flight"] = attribution
        # A WebSocket route carries the same `@authenticated`/`@roles`/
        # `@permissions`/`@authorize` metadata an HTTP route does. Handshake-safe
        # ingress hooks run first, so Host/proxy/session policy cannot be bypassed
        # by switching protocols and session-backed auth sees the loaded session.
        identity: Identity | None = None
        requirement = requirement_for(handler)
        # One question, asked in one place: `needs_backend` now covers the
        # checks this used to name itself, and the second-factor window it did
        # not name -- see `AuthRequirement.access_level`.
        needs_auth = requirement.needs_backend
        websocket_hooks = self._websocket_hooks
        request: Request | None = None
        if websocket_hooks or needs_auth:
            # The handshake is a GET. Keep one Request for ingress and auth so
            # session state published by middleware reaches the backend.
            request = Request(
                {**scope, "method": "GET"},
                receive,
                path_params,
                self._limits,
                app=self,
            )
        if request is not None:
            for before, is_sync in websocket_hooks:
                try:
                    candidate = before(request) if is_sync else await before(request)
                except Exception:  # noqa: BLE001 -- security ingress fails closed
                    await send({"type": "websocket.close", "code": 1008})
                    return
                if candidate is not None:
                    await send({"type": "websocket.close", "code": 1008})
                    return
        if needs_auth:
            if request is None:
                raise RuntimeError("WebSocket authentication request was not constructed")
            try:
                await self._authorize_request(request, requirement)
            except Exception:  # noqa: BLE001 -- security ingress fails closed
                # `Exception`, not `HTTPException`, and for the same reason the
                # ingress hooks three lines above catch broadly: a handshake
                # that cannot be authorized is refused, whatever went wrong.
                # Catching only the refusal meant a backend raising anything
                # else escaped the application with the handshake neither
                # accepted nor closed -- which is not a 500 here, because there
                # is no response to turn into one. The peer simply waits for a
                # frame that never comes, and the exception is swallowed into an
                # application task nobody awaits.
                await send({"type": "websocket.close", "code": 1008})
                return
            identity = request.identity
        websocket = WebSocket(scope, receive, send, path_params, identity=identity)
        try:
            await cast("WebSocketHandler", handler)(websocket)
        except WebSocketDisconnect:
            # The peer left; nothing further to send. Not a failure, so the
            # session's buffered records are discarded like a healthy request's.
            _log_finish_session(promoted=False)
            return
        except BaseException:
            # Anything else ended the session badly -- a handler that raised, or
            # a cancellation tearing the task down. Publish the verbose records
            # for exactly this session, then let it propagate: the promotion is
            # a side effect of the failure, never a substitute for reporting it.
            _log_finish_session(promoted=True)
            raise
        _log_finish_session(promoted=False)

    async def _handle_exception(
        self, request: Request, error: Exception
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse:
        """Turn an exception into a response. The dispatch error boundary.

        Every `except Exception` in dispatch funnels here, and each is waived
        against BLE001 pointing at this docstring. The breadth is the contract,
        not an oversight: the code being guarded is *the caller's* -- middleware
        hooks, the route handler, the auth backend, exception handlers themselves
        -- so there is no set of types to name, and an ASGI application that
        raises must still produce a response rather than drop the connection.

        Nothing is swallowed. The error becomes a registered handler's response,
        a problem+json for `HTTPException`/`ValidationError`, or a 500 -- and
        in `debug` the type and message go in the body. It is visible to the
        client on every path.

        **A registered handler that itself raises does not escape.** Rendering
        runs inside this boundary, so a broken exception handler, status handler,
        fallback handler or validation formatter still produces a 500 problem
        document instead of leaving the request with no response at all. That
        failure is a bug in application code, so it is not merely survived: it
        increments `exception_handler_errors` and is logged with its traceback to
        the `wreath` logger. This is the only place the framework can report it.

        `BaseException` is deliberately not caught anywhere in dispatch, so
        `CancelledError` still unwinds a request that is being torn down and
        `KeyboardInterrupt`/`SystemExit` still end the process.
        """
        try:
            return await self._render_exception(request, error)
        except Exception as failure:  # the handler is the caller's
            # Counted and logged, then answered. The waiver: the code that just
            # raised is a *user-registered renderer*, so there is no set of types
            # to name, and returning nothing would hang the request. Degrading
            # silently is the shape AGENTS.md forbids, so the failure gets a
            # counter and a traceback rather than a shrug.
            import logging

            self.exception_handler_errors += 1
            logging.getLogger("wreath").exception(
                "exception handler for %s raised; answering 500",
                type(error).__name__,
                exc_info=failure,
            )
            if self.debug:
                return ProblemResponse(
                    status=500,
                    detail=f"exception handler raised {type(failure).__name__}: {failure}",
                )
            return ProblemResponse(status=500, detail="Internal Server Error")

    def _allowed_methods(
        self, method: str, path: str, host: str = ""
    ) -> tuple[str, ...]:
        """Methods other than `method` that this `path` does answer, for `Allow`.

        Called only after the route table, the static mounts and the CORS
        preflight fallback have all missed, so this runs on a request that is
        already an error and never on the hot path. It asks the compiled table
        one classification per *registered* method -- the distinct set across the
        whole application, which is at most the handful of HTTP verbs the routes
        actually use -- so it costs nothing to a router that simply has no route
        at this path, which is the common miss.

        Returns an empty tuple when nothing answers this path, which is the real
        404. A `GET` route also answers `HEAD` (dispatch sends the headers with
        no body), so `HEAD` is listed with it even though nothing registered it.
        """
        classify = self.router.classify
        allowed = []
        for candidate in self._route_methods:
            if candidate == method:
                continue
            dynamic_matcher = self._dynamic_matcher
            if (
                dynamic_matcher is not None
                and dynamic_matcher.match(candidate, path, host) is not None
            ):
                allowed.append(candidate)
            elif classify(candidate, path)[0] != 0:
                allowed.append(candidate)
        if "GET" in allowed and "HEAD" not in allowed and method != "HEAD":
            allowed.append("HEAD")
        return tuple(allowed)

    async def _render_exception(
        self, request: Request, error: Exception
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse:
        """Choose and build the response for `error`. Called only under the boundary.

        Split out from `_handle_exception` so every user-supplied renderer it
        consults -- exception handler, validation formatter, status handler,
        fallback handler -- sits inside one `try`, and so no single one of them
        can end a request without a response.
        """
        # Guarded: most applications register no exception handlers at all, and
        # the walk was paying one dict lookup per class in the MRO to find that
        # out on every error response.
        if self._exception_handlers:
            for error_type in type(error).__mro__:
                handler = self._exception_handlers.get(error_type)
                if handler is not None:
                    return _coerce_response(await handler(request, error))
        if isinstance(error, ValidationError):
            formatter = self._validation_formatter
            if formatter is None:
                return ProblemResponse(
                    status=422,
                    detail="Request validation failed",
                    extensions={"errors": error.errors},
                )
            return ProblemResponse(formatter(error.errors, request))
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
        # Guarded because this is reachable from the request path (`__call__`
        # compiles lazily when the route table is dirty). Under asyncio the body
        # never awaits, so two requests cannot interleave inside it -- but
        # AGENTS.md treats free-threading as a separately tested execution mode,
        # and there two threads genuinely can. The lock costs one uncontended
        # acquire on the rare compile, and nothing at all on the steady state,
        # where `_dirty` is already False.
        with self._compile_lock:
            if not self._dirty:
                return          # another caller compiled while we waited
            self._compile_routes_locked()

    def _reject_session_after_authentication(self, app_middleware: tuple[Any, ...]) -> None:
        """Refuse a session-reading backend behind route-scoped session middleware.

        `SessionIdentityBackend` reads `request.state.session` during
        authentication. `SessionMiddleware` is route middleware by design, so it
        is compiled into a route's tape and runs *after* authorization has
        already passed -- deliberately, so a miss or a static file never pays to
        decode a cookie. Register the two together and the session the backend
        needs is published after the backend has been asked for an identity, so
        a caller holding a perfectly valid session cookie is refused on every
        protected route.

        The failure is a 401, which reads as "my login is broken" rather than
        "these two are wired in the wrong order", and nothing else in the request
        distinguishes it from a genuine anonymous call. So it is refused here,
        naming the remedy, rather than left to be discovered in production.

        **Every route-scoped registration is examined, not just
        `add_middleware()`.** This used to read `self._middleware` alone, so
        `Router(middleware=[SessionMiddleware(...)])` -- and
        `@app.get(..., middleware=[...])`, and `include_router(middleware=[...])`
        -- walked past a refusal that had nothing to look at and shipped the
        exact 401 it exists to prevent: sign-in answered 200 and `/me` answered
        401. A refusal that holds on one supported wiring and not another reports
        safety while providing none (docs/decisions/0024). Nesting needs no
        special case: `Router.routes` folds an included router's middleware into
        each route before the application ever sees it, so a session middleware
        three routers deep arrives in that route's own tuple.

        Raises:
            TypeError: The authentication backend needs a session and the session
                middleware is registered on a route pipeline -- the application's,
                a router's, or one route's.
        """
        backend = self._auth_backend
        if backend is None or not getattr(backend, "requires_session", False):
            return
        offender = _first_session_publisher(app_middleware)
        where = "add_middleware()"
        if offender is None:
            for definition in self._routes:
                offender = _first_session_publisher(definition.middleware)
                if offender is not None:
                    where = f"middleware=[...] on {definition.path}"
                    break
        if offender is None:
            return
        name = type(offender).__name__
        raise TypeError(
            f"{type(backend).__name__} reads request.state.session during "
            f"authentication, but {name} was registered with {where}, "
            "which runs it after authorization -- every protected route would "
            f"answer 401 with a valid session cookie. Register it with "
            f"add_global_middleware({name}(...)) so the session is published "
            "before authentication runs."
        )

    def _compile_routes_locked(self) -> None:
        binding_specs = self._application_image.binding_specs()
        router = CompiledRouter(self._routing)
        dynamic_matcher = _DynamicMatcher()
        app_middleware = tuple(
            item[2] for item in sorted(self._middleware, key=lambda item: (item[0], item[1]))
        )
        global_middleware = tuple(
            item[2]
            for item in sorted(self._global_middleware, key=lambda item: (item[0], item[1]))
        )
        self._reject_session_after_authentication(app_middleware)
        # Dense directional programs: request dispatch never scans a missing
        # before/after slot. Each before records the exact after-prefix active
        # before and after it completes, preserving partial unwind semantics.
        compiled_before_hooks: list[tuple[Any, bool, int, int]] = []
        compiled_after_hooks: list[tuple[Any, int]] = []
        for item in global_middleware:
            before_sync = getattr(item, "before_sync", None)
            before = (
                before_sync
                if before_sync is not None
                else getattr(item, "before", None)
            )
            after_sync = getattr(item, "after_sync", None)
            after_inplace = getattr(item, "after_inplace", None)
            after = (
                after_inplace
                if after_inplace is not None
                else (
                    after_sync
                    if after_sync is not None
                    else getattr(item, "after", None)
                )
            )
            error_afters = len(compiled_after_hooks)
            if after is not None:
                mode = (
                    2
                    if after_inplace is not None
                    else (1 if after_sync is not None else 0)
                )
                compiled_after_hooks.append((after, mode))
            if before is not None:
                compiled_before_hooks.append(
                    (
                        before,
                        before_sync is not None,
                        error_afters,
                        len(compiled_after_hooks),
                    )
                )
        self._global_before_hooks = tuple(compiled_before_hooks)
        self._global_after_hooks = tuple(compiled_after_hooks)
        websocket_hooks: list[tuple[Any, bool]] = []
        for item in global_middleware:
            websocket_hook = getattr(item, "before_websocket", None)
            if websocket_hook is not None:
                websocket_hooks.append((websocket_hook, False))
            elif getattr(item, "websocket_scope", False):
                before_sync = getattr(item, "before_sync", None)
                if before_sync is not None:
                    websocket_hooks.append((before_sync, True))
                elif (before := getattr(item, "before", None)) is not None:
                    websocket_hooks.append((before, False))
        self._websocket_hooks = tuple(websocket_hooks)
        stage_hooks = {
            stage: tuple(
                hook
                for item in global_middleware
                if (hook := getattr(item, stage, None)) is not None
            )
            for stage in ("miss", "pre_auth", "identity", "action")
        }
        self._stage_hooks = {stage: hooks for stage, hooks in stage_hooks.items() if hooks}
        self._has_global_http_hooks = bool(
            compiled_before_hooks or compiled_after_hooks or self._stage_hooks
        )
        requirements = [
            merge_requirements(route.requirement, requirement_for(route.endpoint))
            for route in self._routes
        ]
        self._compile_capabilities(requirements)
        handler_requirements: dict[Any, AuthRequirement] = {}
        # Compiled handler -> (method, path) so a later telemetry join can map
        # each route to its metadata-image IDs without re-walking the router.
        flight_route_keys: dict[Any, tuple[str, str]] = {}
        for definition, requirement, binding_spec in zip(
            self._routes, requirements, binding_specs, strict=True
        ):
            # Typed-signature binding compiles once here; request-only
            # handlers come back unchanged.
            endpoint = compile_binder(
                definition.endpoint,
                definition.path,
                databases=self._databases,
                orm_registries=self._orm_registries,
                dependencies=definition.dependencies,
                binding_spec=binding_spec,
                app_scope=self._app_scope,
            )
            returns = (
                binding_spec.returns
                if binding_spec is not None
                else _return_annotation(definition.endpoint)
            )
            endpoint = compile_response_validator(endpoint, returns)
            chain = app_middleware + definition.middleware
            if definition.status_code != 200 and not definition.response_only:
                endpoint = _ensure_response(endpoint, definition.status_code)
            elif chain and not definition.response_only:
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
                if definition.host is not None or ":path}" in definition.path:
                    dynamic_matcher.add(
                        definition.path, method, definition.host, compiled
                    )
                else:
                    router.add(definition.path, method, compiled, access_clauses)
                handler_requirements[compiled] = requirement
                flight_route_keys[compiled] = (method, definition.path)
        # WebSocket routes are attributable too: the matched handler joins to a
        # WEBSOCKET route in the metadata image. They carry no HTTP plan.
        for ws_path, ws_handler in self._ws_routes:
            flight_route_keys[ws_handler] = ("WEBSOCKET", ws_path)
        router.compile()
        if self._ws_router is not None:
            self._ws_router.compile()
        self._handler_requirements = handler_requirements
        self._auth_handlers = frozenset(
            compiled
            for compiled, requirement in handler_requirements.items()
            if requirement.needs_backend
        )
        # Sorted for a stable `Allow` header; deduplicated because a method
        # appears once per route that declares it.
        self._route_methods = tuple(
            sorted({method for route in self._routes for method in route.methods})
        )
        self._flight_route_keys = flight_route_keys
        self._flight_route_ids = None  # rebuilt lazily against the new routes
        self.router = router
        self._dynamic_matcher = dynamic_matcher if dynamic_matcher._routes else None
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
        # Stamp each dependency's metadata-image ID onto the live object so
        # phase markers can attribute DB_QUERY/HTTP_CLIENT phases without a
        # per-call name lookup. Runs before any armed request reaches a
        # handler: dispatch builds this mapping in the attribution block.
        for named in image.databases:
            database = self._databases.get(named.name)
            if database is not None:
                database._flight_dep_id = named.entry_id
        for named in image.clients:
            client = self._http_clients.get(named.name)
            if client is not None:
                client._flight_dep_id = named.entry_id
        # Models get the same treatment, so an ORM read can attribute its
        # ORM_HYDRATE phase to a model without formatting a name per query.
        # That attribution is what `wreath doctor n-plus-one` reads back.
        model_ids = {named.name: named.entry_id for named in image.models}
        for registry in self._orm_registries.values():
            for spec in getattr(registry, "specs", ()):
                entry_id = model_ids.get(spec.model_type.__qualname__)
                if entry_id is not None:
                    registry._flight_model_ids[spec.model_type] = entry_id
        self._flight_route_ids = mapping
        return mapping

    def _set_flight_recording(self, plan: Any, registry: Any) -> None:
        """Install (or clear) the forensic capture plan + runtime arm registry.

        The server calls this at startup under a Forensic recorder, and clears it
        on shutdown. With `plan` None the request-path capture seam stays a
        single not-taken branch (every non-Forensic server, and Forensic before a
        recording policy is compiled).
        """
        self._flight_capture_plan = plan
        self._flight_arm_registry = registry

    def _capture_headers(
        self, capture: Any, arms: Any, headers: Any, field_class: int
    ) -> None:
        """Capture the headers both the ceiling and an active arm permit.

        The descriptor id comes from the startup ceiling plan (that is the
        numbering the recording's name table was written with); the disposition
        narrows to what the active arms actually grant. A header the ceiling drops
        can never be broadened by an arm, so it is skipped before the arm lookup.
        """
        ceiling_header = self._flight_capture_plan.header
        for name, value in headers:
            decoded = name.decode("latin-1")
            ceiling_rule = ceiling_header(decoded)
            if ceiling_rule is None:
                continue
            disposition = _narrow_header(arms, decoded)
            if disposition is None:
                continue
            capture(field_class, ceiling_rule.descriptor_id, int(disposition), value)

    def _capture_request(self, scope: Any, request: Request) -> Any:
        """Capture policy-approved request headers for an armed Forensic request.

        Reached only when the native context reported `flight == 2` (armed) and
        a capture plan is installed -- i.e. only on the Forensic-sampled subset of
        requests. Capture is gated on *active* runtime arms, narrows to each arm's
        compiled plan (within the startup ceiling), and counts one match per
        active arm. The native `_flight_capture` is deny-by-default and redacts
        each field, so a header no arm permits never leaves recorder memory.
        Returns the active-arm snapshot (shared with the completion and dependency
        seams) or `None` when nothing is armed.
        """
        registry = self._flight_arm_registry
        arms = registry.active() if registry is not None else ()
        if not arms:
            return None
        capture = scope._flight_capture
        self._capture_headers(capture, arms, request.headers, _CAP_REQUEST_HEADER)
        self._capture_query(capture, arms, request)
        for arm in arms:
            registry.note_match(arm.arm_id)
        return arms

    def _capture_query(self, capture: Any, arms: Any, request: Request) -> None:
        """Capture policy-approved query parameters, in their own descriptor
        namespace. Deny-by-default: an unlisted parameter is never captured. Same
        ceiling-descriptor / arm-narrowing rule as headers."""
        query_string = request.query_string
        if not query_string:
            return
        ceiling_query = self._flight_capture_plan.query
        for name, value in _parse_qs(query_string, 0):
            ceiling_rule = ceiling_query(name)
            if ceiling_rule is None:
                continue
            disposition = _narrow_query(arms, name)
            if disposition is None:
                continue
            capture(
                _CAP_QUERY_PARAM, ceiling_rule.descriptor_id, int(disposition),
                value.encode("utf-8"),
            )

    def _capture_completion(
        self, scope: Any, request: Request, response: Any, arms: Any
    ) -> None:
        """Capture policy-approved response headers and request/response bodies.

        Called only when `_capture_request()` found active arms, so the match
        is already counted; this adds the response side under the same arm
        snapshot. Response headers resolve against the same direction-agnostic
        redaction rules as the request headers. The request body is captured only
        from what the handler already buffered -- never a fresh read that would
        consume the stream or change behavior -- and only plain-`bytes`
        responses are captured (streaming/file bodies fall back to nothing, never
        raw, per the redaction rules).
        """
        capture = scope._flight_capture
        headers = getattr(response, "headers", None)
        if headers is not None:
            self._capture_headers(capture, arms, headers, _CAP_RESPONSE_HEADER)
        request_body = _narrow_body(arms, _FC_REQUEST_BODY)
        if request_body is not None:
            buffered = request._body
            if isinstance(buffered, (bytes, bytearray)):
                _capture_body(capture, _CAP_REQUEST_BODY, request_body, buffered)
        response_body = _narrow_body(arms, _FC_RESPONSE_BODY)
        if response_body is not None:
            body = getattr(response, "body", None)
            if isinstance(body, (bytes, bytearray)):
                _capture_body(capture, _CAP_RESPONSE_BODY, response_body, body)

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
                    # Disjunctive normal form: every `any` check multiplies the
                    # clause list by its value count, so K such checks of V
                    # values each produce V**K clauses. That is what DNF costs
                    # -- deduplication cannot reduce it when the values are
                    # distinct -- and the cost is not only declaration-time
                    # memory: `_eligible` scans every clause on every request,
                    # so an unbounded expansion silently turns route matching
                    # into a per-request linear scan of that same product.
                    #
                    # Refused rather than expanded, because an app that would
                    # take minutes to start and then match routes slowly is
                    # worse than one that names the problem at declaration.
                    # Checked *before* the multiplication, not after it: a dozen
                    # checks would otherwise have to materialise millions of
                    # clauses in order to discover it had too many.
                    product = len(clauses) * len(masks)
                    if product > MAX_ACCESS_CLAUSES:
                        raise ValueError(
                            f"this route's authorization expands to {product} "
                            f"clauses (limit {MAX_ACCESS_CLAUSES}). Combining "
                            "several mode='any' checks multiplies them, and they "
                            "accumulate across nested routers; express the "
                            "requirement as one check, or as a Cedar policy."
                        )
                    clauses = [clause | mask for clause in clauses for mask in masks]
                    # Deduplicate as we go rather than only at the end: two
                    # checks that share values (a role hierarchy repeated on a
                    # router and its parent) collapse here instead of
                    # multiplying first.
                    clauses = list(dict.fromkeys(clauses))
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
            except Exception as error:  # noqa: BLE001 -- see _handle_exception
                return await self._handle_exception(request, error)
            if candidate is not None:
                return _coerce_response(candidate)
        return None

    async def _authorize_request(
        self, request: Request, requirement: AuthRequirement, backend: Any = None
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse | None:
        """Run authentication and authorization before route middleware.

        `backend` overrides the app-wide auth backend for route-scoped
        authentication (e.g. the API-docs subsystem); it defaults to
        `self._auth_backend` so ordinary routes are unaffected.
        """
        auth_backend = backend if backend is not None else self._auth_backend
        identity = request.identity
        if identity is None:
            if "pre_auth" in self._stage_hooks:
                stage_response = await self._run_stage("pre_auth", request)
                if stage_response is not None:
                    return stage_response
            if auth_backend is not None:
                identity = await auth_backend.authenticate(request)
                request._set_identity(identity)
                if "identity" in self._stage_hooks:
                    stage_response = await self._run_stage("identity", request)
                    if stage_response is not None:
                        return stage_response
        if identity is None:
            if requirement.access_level == 0:
                # `identify()` only: the backend was asked and answered nobody,
                # which is a value rather than a failure, and this endpoint asks
                # the caller for nothing. Asked as `access_level` rather than as
                # `not authenticated` so that every field that can refuse a
                # caller is consulted: a requirement carrying role checks or a
                # second-factor window but not the `authenticated` flag its
                # decorator would have set is refused here, rather than admitted
                # anonymously with its checks silently skipped.
                return None
            challenge = (
                None if auth_backend is None else auth_backend.challenge(request)
            )
            raise Unauthorized(challenge=challenge)
        if requirement.second_factor is not None:
            # Step-up, checked before roles and policies: the question "did this
            # person prove a factor lately" does not depend on any of them, and
            # answering it first keeps the remediation unambiguous. A 403 rather
            # than a 401 -- re-authenticating changes nothing, proving a factor
            # does -- with the reason naming what to do about it.
            age = second_factor_age(identity, _wall_clock())
            if age is None or age > requirement.second_factor:
                raise Forbidden("second_factor_required")
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

    def enable_api_docs(
        self,
        *,
        path: str = "/docs",
        spec_path: str = "/openapi.json",
        environments: Iterable[str] | None = None,
        env: str | None = None,
        auth: Any = None,
        authorize: Callable[[Any], Any] | None = None,
        authenticated: bool = False,
        permissions: Iterable[str] = (),
        try_it_out: bool = False,
        title: str = "Wreath",
        version: str = "0.1.0",
    ) -> bool:
        """Serve a self-contained API-docs page + the OpenAPI document, fail-closed.

        Both are rendered from the same signature inspection that drives request
        binding, so the page, the spec, and the typed clients cannot drift. No
        external/CDN assets are used.

        Gating is declarative and fail-closed:

        * `environments` -- if set, the routes are registered *only* when the
          current environment (`env` arg, else `WREATH_ENV`, else
          `"production"`) is listed. Otherwise nothing is registered and both
          paths 404 -- no existence leak. Forgetting to set an env therefore
          means production, which means docs OFF.
        * `authenticated` / `permissions` / `authorize` -- require auth on
          the two routes, enforced (401/403) by a route-scoped guard. `authorize`
          is a decorator such as `wreath.authorization.authorize`.
        * `auth` -- a backend that authenticates ONLY these two routes,
          independent of `configure_auth()` (no global side effect). When it
          and the other auth args are all omitted, the docs are open within the
          allowed environments (environment gating is the only guard). A live
          try-it-out console inherits exactly this gate.

        Returns `True` when the routes were registered, `False` when the
        environment gate withheld them.
        """
        import os
        import secrets

        from ._auth.requirements import add_authenticated, add_permissions
        from .openapi import generate_openapi, render_docs_body, render_docs_shell

        current_env = env or os.environ.get("WREATH_ENV") or "production"
        if environments is not None and current_env not in tuple(environments):
            return False

        permission_values = frozenset(permissions)
        spec_state: dict[str, Any] = {}
        body_state: dict[str, Any] = {}

        def spec_bytes() -> bytes:
            # Keyed on the route tuple's identity, not its length: swapping one
            # route for another leaves the count identical, and the cached spec
            # then described routes that no longer existed. The same trap
            # `_vocabulary_reader` in `_auth/permissions` avoids by comparing.
            key = tuple(self._routes)
            if spec_state.get("bytes") is None or spec_state.get("key") != key:
                spec_state["bytes"] = _json_dumps(
                    generate_openapi(self, title=title, version=version)
                )
                spec_state["key"] = key
            return spec_state["bytes"]

        def body_html() -> str:
            key = tuple(self._routes)
            if body_state.get("html") is None or body_state.get("key") != key:
                body_state["html"] = render_docs_body(
                    self, title=title, version=version, try_it_out=try_it_out
                )
                body_state["key"] = key
            return body_state["html"]

        async def openapi_spec(request: Request) -> Any:
            return Response(spec_bytes(), media_type=b"application/json")

        async def docs(request: Request) -> Any:
            nonce = secrets.token_urlsafe(16)
            page = render_docs_shell(
                title=title,
                version=version,
                spec_path=spec_path,
                nonce=nonce,
                body=body_html(),
                try_it_out=try_it_out,
            )
            # The docs page carries its OWN CSP with a per-response nonce: the
            # default `default-src 'self'` would otherwise block the single inline
            # <style>/<script>. connect-src 'self' bounds try-it-out to same-origin.
            csp = (
                f"default-src 'self'; style-src 'nonce-{nonce}'; "
                f"script-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'self'"
            ).encode("ascii")
            return Response(
                page.encode("utf-8"),
                media_type=b"text/html; charset=utf-8",
                headers=[(b"content-security-policy", csp)],
            )

        # Build the docs auth requirement on a throwaway marker so it is NOT
        # compiled into the global pre-route auth path (which uses the global
        # backend). We enforce it ourselves in a route-scoped guard with `auth`
        # as the backend, so `auth` guards only these two routes independent of
        # `configure_auth`. With no auth args, the requirement is empty -> open.
        def _marker() -> None:  # pragma: no cover - metadata carrier only
            return None

        if authorize is not None:
            _marker = authorize(_marker)
        if permission_values:
            _marker = add_permissions(_marker, permission_values, "all")
        elif authenticated or auth is not None:
            _marker = add_authenticated(_marker)
        docs_requirement = requirement_for(_marker)
        needs_guard = bool(
            docs_requirement.authenticated
            or docs_requirement.role_checks
            or docs_requirement.permission_checks
            or docs_requirement.policies
        )

        def _scoped(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
            async def guarded_handler(request: Request) -> Any:
                if needs_guard:
                    denied = await self._authorize_request(
                        request, docs_requirement, backend=auth
                    )
                    if denied is not None:
                        return denied
                return await handler(request)

            return guarded_handler

        self.get(spec_path)(_scoped(openapi_spec))
        self.get(path)(_scoped(docs))
        return True

    def enable_docs(
        self,
        *,
        docs_path: str = "/docs",
        spec_path: str = "/openapi.json",
        title: str = "Wreath",
        version: str = "0.1.0",
        environments: Iterable[str] | None = NON_PRODUCTION_ENVIRONMENTS,
    ) -> bool:
        """Backwards-compatible alias for `enable_api_docs()`.

        Serves the self-contained docs page and OpenAPI document **outside
        production**. The gate is the difference from what this used to do: the
        alias registered both routes unconditionally, so the shorter, older,
        more-copied spelling was the one that published the whole API surface
        from a production deployment. Pass `environments=None` to register
        everywhere, or use `enable_api_docs()` for auth as well as env
        gating.

        Returns whether the routes were registered.
        """
        return self.enable_api_docs(
            path=docs_path,
            spec_path=spec_path,
            title=title,
            version=version,
            environments=environments,
        )

    def on_startup(self, handler: LifespanHandler) -> LifespanHandler:
        """Run `handler(app)` during lifespan startup, in registration order."""
        self._startup_handlers.append(handler)
        return handler

    def on_shutdown(self, handler: LifespanHandler) -> LifespanHandler:
        """Run `handler(app)` during lifespan shutdown, in registration order."""
        self._shutdown_handlers.append(handler)
        return handler

    async def _close_all(
        self, steps: list[tuple[str, Callable[[], Any]]]
    ) -> BaseException | None:
        """Run every teardown step in order, even when one of them raises.

        Releasing resources on a failing path is the one place where stopping at
        the first error is strictly wrong: the steps are independent, nothing
        later depends on an earlier one succeeding, and whatever is skipped is
        never released at all -- on the startup path the server is told startup
        failed and never calls shutdown. A refusing `close()` used to abandon
        every pool queued behind it *and* the lifespan reply itself.

        Each failure is counted on `lifespan_teardown_errors` and logged with its
        traceback. The first is returned so the shutdown path can report it,
        while the startup path keeps reporting the error that caused the failure
        rather than the one raised while cleaning up after it.

        Args:
            steps: `(label, callable)` pairs in teardown order. The callable
                takes no arguments; an awaitable result is awaited.

        Returns:
            The first exception raised by a step, or None if all of them ran
            cleanly.
        """
        import logging

        first: BaseException | None = None
        for label, close in steps:
            try:
                result = close()
                if isawaitable(result):
                    await result
            except Exception as failure:
                # Broad by necessity: these are user-supplied and driver-supplied
                # closers, so there is no set of types to name, and the whole
                # point of this loop is that one failure must not stop the rest.
                # It is not a swallow -- counted, logged with its traceback, and
                # the first one is handed back to the caller.
                self.lifespan_teardown_errors += 1
                logging.getLogger("wreath").exception(
                    "lifespan teardown step %r failed; continuing", label
                )
                if first is None:
                    first = failure
        return first

    async def _lifespan(self, receive: Any, send: Send) -> None:
        while True:
            message = await receive()
            message_type = message["type"]
            if message_type == "lifespan.startup":
                # Names travel with the objects so a teardown failure names the
                # registration that refused, not just its type.
                started_databases: list[tuple[str, Any]] = []
                started_clients: list[tuple[str, Any]] = []
                try:
                    for name, database in self._databases.items():
                        await database.start()
                        started_databases.append((name, database))
                    # Wreath's own tables, before anything that uses them. The
                    # job queue's first enqueue must not be where a missing
                    # `wreath.jobs` is discovered, and a deployment that opted
                    # out must be refused here rather than at 3am -- so this
                    # runs ahead of the ORM check, the clients, the supervisor
                    # and every user startup handler.
                    await self._bootstrap_schema()
                    if self._orm_registries:
                        # Ordered explicitly rather than through a synthetic
                        # startup handler: the schema must be checked once the
                        # database is up but before user code queries it.
                        from .orm.introspection import (
                            resolve_extension_types,
                            validate_registry,
                        )

                        for registry in self._orm_registries.values():
                            # Extension type OIDs first, and unconditionally:
                            # they are assigned by CREATE EXTENSION rather than
                            # compiled in, the codec cannot frame a value
                            # without them, and validate_schema="off" must not
                            # be a way to skip that. A registry declaring none
                            # does no I/O here.
                            await resolve_extension_types(registry)
                            await validate_registry(registry)
                    for name, client in self._http_clients.items():
                        await client.start()
                        started_clients.append((name, client))
                    # Supervised jobs/messaging start after their dependencies
                    # (databases, clients) and before user startup handlers, so a
                    # startup handler may enqueue onto a running runner.
                    if self._job_runners or self._message_buses:
                        from .services import Supervisor

                        supervisor = Supervisor()
                        for runner in self._job_runners.values():
                            supervisor.add(runner)
                        for bus in self._message_buses.values():
                            supervisor.add(bus)
                        await supervisor.start()
                        self._supervisor = supervisor
                    for handler in self._startup_handlers:
                        await handler(self)
                except Exception as error:  # noqa: BLE001 - reported to the server
                    # Everything started so far is released here or never: the
                    # server is about to be told startup failed, and it will not
                    # call shutdown. `_close_all` is what makes that total.
                    supervisor, self._supervisor = self._supervisor, None
                    steps: list[tuple[str, Callable[[], Any]]] = []
                    if supervisor is not None:
                        steps.append(("supervisor", supervisor.stop))
                    # App-scoped dependencies first, as on the ordinary shutdown
                    # path: a startup handler may have opened one before the
                    # failure, and it was being left open along with whatever it
                    # holds -- a connection, a file, a client.
                    steps.append(("app scope", self._app_scope.aclose))
                    # Static mounts are opened by `static()` at registration, so
                    # they exist even when startup fails before anything else
                    # started -- and the server will not call shutdown.
                    steps += [
                        (f"static mount {prefix!r}", close)
                        for prefix, handler in reversed(self._static_matcher._mounts)
                        if (close := getattr(handler, "close", None)) is not None
                    ]
                    steps += [
                        (f"http client {name!r}", client.close)
                        for name, client in reversed(started_clients)
                    ]
                    steps += [
                        (f"database {name!r}", database.stop)
                        for name, database in reversed(started_databases)
                    ]
                    await self._close_all(steps)
                    # The original failure is what the server needs to see; a
                    # close that also refused is on `lifespan_teardown_errors`.
                    await send(
                        {"type": "lifespan.startup.failed", "message": f"{error!r}"}
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                failure: BaseException | None = None
                try:
                    for handler in self._shutdown_handlers:
                        await handler(self)
                except Exception as error:  # noqa: BLE001 - reported to the server
                    # Held, not returned on: a shutdown handler that raises is
                    # still a shutdown, and the resources below are released here
                    # or never. Returning early leaked every pool the app owned.
                    failure = error
                # Stop supervised work before the resources it depends on:
                # stop fetching, drain in-flight, release leases (design 01).
                supervisor, self._supervisor = self._supervisor, None
                steps: list[tuple[str, Callable[[], Any]]] = []
                if supervisor is not None:
                    steps.append(("supervisor", supervisor.stop))
                # App-scoped dependencies before the resources they were built
                # from: an app-scoped generator may still want to talk to a
                # database or HTTP client on the way out.
                steps.append(("app scope", self._app_scope.aclose))
                # Static mounts hold a root descriptor each, opened when `static()`
                # registered them. Nothing else can reach the instance -- it is
                # stored only as an opaque handler -- so this is the only place
                # `StaticFiles.close()` is reachable through the public API, and
                # its own docstring's "call it at shutdown" is otherwise advice
                # nobody can take.
                steps += [
                    (f"static mount {prefix!r}", close)
                    for prefix, handler in reversed(self._static_matcher._mounts)
                    if (close := getattr(handler, "close", None)) is not None
                ]
                steps += [
                    (f"object store {name!r}", close)
                    for name, store in reversed(tuple(self._object_stores.items()))
                    if (close := getattr(store, "close", None)) is not None
                ]
                steps += [
                    (f"http client {name!r}", client.close)
                    for name, client in reversed(tuple(self._http_clients.items()))
                ]
                steps += [
                    (f"database {name!r}", database.stop)
                    for name, database in reversed(tuple(self._databases.items()))
                ]
                teardown_failure = await self._close_all(steps)
                if failure is None:
                    failure = teardown_failure
                if failure is not None:
                    await send(
                        {"type": "lifespan.shutdown.failed", "message": f"{failure!r}"}
                    )
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return


def _check_set(actual: frozenset[str], requirement: SetRequirement) -> bool:
    # `issubset`, not `<=`: the operator form demands a set on the right and
    # raises `TypeError` for any other collection, while `isdisjoint` on the
    # `any` branch below has always accepted any iterable. A backend that passes
    # a roles claim through unconverted (a JSON array decodes to a list) thus
    # made every *authorized* request a 500 under `mode="all"` and a 200 under
    # `mode="any"`. Both branches now read the same collections.
    if requirement.mode == "all":
        return requirement.values.issubset(actual)
    return not requirement.values.isdisjoint(actual)


_NOT_FOUND = ProblemResponse(status=404, detail="Not Found")


def _ensure_response(endpoint: Handler, status: int = 200) -> Handler:
    async def coerced(
        request: Request,
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse:
        return _coerce_response(await endpoint(request), status=status)

    coerced.__name__ = getattr(endpoint, "__name__", "coerced")
    coerced.__qualname__ = getattr(endpoint, "__qualname__", "coerced")
    return coerced


def _coerce_response(
    value: Any, *, status: int = 200
) -> Response | StreamingResponse | FileResponse | PreparedResponse:
    """Turn a handler's return value into a response object.

    Two deliberate tiers, not redundant checks: the exact-type `kind is` arms
    are the common fast path (str/dict/bytes build a response in one frame,
    skipping the ladder); the `isinstance` arms below the response-type check
    then catch the rarer subclasses (a str/bytes subclass, or a dict/list/scalar
    for JSON) that the exact-type tests miss.
    """
    kind = type(value)
    if kind is str:
        return coerce_text(value) if status == 200 else TextResponse(value, status=status)
    if kind is dict:
        return coerce_json(value) if status == 200 else JSONResponse(value, status=status)
    if kind is bytes:
        return coerce_bytes(value) if status == 200 else Response(value, status=status)
    if isinstance(value, (Response, StreamingResponse, FileResponse, PreparedResponse)):
        return value
    if isinstance(value, bytes):
        return Response(value, status=status)
    if isinstance(value, str):
        return TextResponse(value, status=status)
    if isinstance(value, (dict, list, tuple, int, float, bool)) or value is None:
        return JSONResponse(value, status=status)
    raise TypeError(f"handlers must return a response-compatible value, got {type(value).__name__}")


def _head_send(send: Send) -> Send:
    async def send_without_body(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            message = {**message, "body": b""}
        await send(message)

    return send_without_body
