"""Wreath ASGI application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from time import monotonic_ns as _monotonic_ns
from typing import Any, Literal, cast

from ._auth.backends import AuthenticationBackend, AuthorizationProvider
from ._auth.models import Identity
from ._auth.requirements import (
    AuthRequirement,
    SetRequirement,
    merge_requirements,
    requirement_for,
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
from ._routing import _CLASSIFYING, Handler, RoutingMode
from ._routing import Router as CompiledRouter
from .binding import (
    AppScope,
    BindingSpec,
    Depends,
    ValidationError,
    compile_binder,
    inspect_handler,
)
from .cache_control import CacheControl
from .exceptions import Forbidden, HTTPException, NotFound, Unauthorized
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
    ``(field_class, data)`` and is propagated via ``capture_marker`` to the
    PostgreSQL and HTTP-client seams."""
    disposition, limit = rule
    disposition_int = int(disposition)
    max_bytes = limit if disposition is _CAP_DISP_RAW else 0
    capture = scope._flight_capture

    def _capture_dependency(field_class: int, data: bytes | bytearray) -> None:
        capture(field_class, 0, disposition_int, bytes(data), max_bytes)

    return _capture_dependency

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
                inspect_handler(definition.endpoint, definition.path)
                for definition in routes
            )
            self._analyzed = True
        return self._binding_specs


class _StaticMatcher:
    """Mount prefixes in registration order; the first that matches wins.

    Precedence is first-registration, not longest-prefix, so scanning the
    mounts in order and taking the first hit is exact -- the scan stops at the
    winner rather than having to see every candidate.

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
            if path.startswith(prefix):
                return handler, {"path": path[len(prefix):]}
        return None


class Wreath:
    """A compact ASGI application with an intentionally provisional API."""

    __slots__ = (
        "_all_capability_mask",
        "_application_image",
        "_auth_backend",
        "_app_scope",
        "_authorizer",
        "_capabilities",
        "_classify",
        "_crud_enabled",
        "_dirty",
        "_databases",
        "_exception_handlers",
        "_flight_route_ids",
        "_flight_route_keys",
        "_flight_capture_plan",
        "_flight_arm_registry",
        "_limits",
        "_fallback_exception_handler",
        "_global_hooks",
        "_global_middleware",
        "_handler_requirements",
        "_http_clients",
        "_job_runners",
        "_match",
        "_message_buses",
        "_object_stores",
        "_middleware",
        "_middleware_order",
        "_oidc_providers",
        "_orm_registries",
        "_supervisor",
        "_preflight_fallback",
        "_probe",
        "_resolve",
        "_routes",
        "_routing",
        "_shutdown_handlers",
        "_startup_handlers",
        "_stage_hooks",
        "_static_matcher",
        "_validation_formatter",
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
        routing: RoutingMode = "bitset",
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
        self._startup_handlers: list[LifespanHandler] = []
        self._shutdown_handlers: list[LifespanHandler] = []
        self._routes: list[RouteDefinition] = []
        self._application_image = _ApplicationImage(self)
        self._middleware: list[tuple[int, int, Middleware]] = []
        self._global_middleware: list[tuple[int, int, Middleware]] = []
        # (before_hook, after_hook, is_sync) per global middleware.
        self._global_hooks: tuple[tuple[Any, Any, bool], ...] = ()
        self._handler_requirements: dict[Any, AuthRequirement] = {}
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
        self._crud_enabled = False
        self._databases: dict[str, Any] = {}
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
        """Register a lifespan-managed object-storage backend (``local`` or ``s3``).

        Exposed on ``app.state.objects_<name>``. An ``s3`` backend owns a pinned
        outbound ``HTTPClient`` started/stopped with the app; ``local`` opens its root
        at registration and is closed on shutdown. Credentials come from
        ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``/``AWS_SESSION_TOKEN`` unless
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

        ``http_client`` is the name of an :meth:`http_client` pinned to the
        issuer origin, or an ``HTTPClient`` instance. Discovery and the first
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
        """Register ``/auth/login`` + ``/auth/callback`` for ``provider``.

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
        """Configure a durable job runner on an existing ``app.postgres()`` database.

        Its workers, sweeper, and scheduler run for the process lifetime, started
        during lifespan after the databases come up. See :mod:`wreath.jobs`.

        Pass a :class:`~wreath.progress.ProgressRegistry` to make the queue
        watchable: :meth:`~wreath.jobs.JobRunner.launch` hands back a task id, a
        handler reports through ``ctx.report()``, and the runner sets the
        terminal state itself. Give the registry the message bus and a job
        running on any worker is watchable from every worker.
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
        """Configure a pub/sub + durable message bus on an ``app.postgres()``
        database. Consumers run for the process lifetime. See :mod:`wreath.messaging`."""
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
            schema_mode=schema_mode,
        )
        self._orm_registries[database] = registry
        self.state.__setattr__(f"orm_{database}", registry)
        self._dirty = True
        return registry

    def flags(self, provider: Any = None, **values: str) -> Any:
        """Register a feature-flag provider on ``app.state.flags``.

        Pass a ``FlagProvider`` (e.g. ``FeatureFlags(...)``), keyword flag values,
        or nothing to build one from the environment (``WREATH_FLAG_*``).
        ``wreath.flags.flags_dependency`` reads ``app.state.flags``.
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
        """Mount liveness (``/health``) + readiness (``/ready``) endpoints."""
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
        """Mount a Prometheus scrape endpoint rendering ``source.snapshot()``."""
        from ._prometheus import metrics_router

        router = metrics_router(source, path=path, namespace=namespace, route_labels=route_labels)
        self.include_router(router)
        return router

    def users(self, store: Any, *, secret: str, **options: Any) -> Any:
        """Mount the user-management lifecycle router (register/login/verify/reset)."""
        from .users import user_router

        router = user_router(store, secret=secret, **options)
        self.include_router(router)
        return router

    def enable_crud(self) -> None:
        """Allow :meth:`crud` to mount auto-generated CRUD routers.

        CRUD is off by default: generating write endpoints from a model is a
        deliberate, app-wide decision, so it must be turned on explicitly before
        any model can opt in.
        """
        self._crud_enabled = True

    def crud(self, model: type, open_session: Any, **options: Any) -> Any:
        """Mount auto-generated CRUD routes for one ``model`` (requires opt-in).

        Off unless :meth:`enable_crud` was called (config-level opt-in) *and* you
        call this per model (model-level opt-in). Sensitive columns are hidden and
        unwritable unless named in ``expose=``. See :mod:`wreath.crud`.
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

        Global middleware must expose ``before``, ``before_sync``, and/or
        ``after`` hooks. Unlike route middleware, it covers misses, static
        files, and authorization failures, so it is suitable for ingress checks
        and response headers.
        """
        if not any(
            hasattr(middleware, name) for name in ("before", "before_sync", "after")
        ):
            raise TypeError(
                "global middleware must expose before, before_sync, and/or after hooks"
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
        self._static_matcher.add(normalized, cast("Handler", handler))

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

    def set_validation_formatter(self, formatter: Any) -> None:
        """Shape 422 bodies with ``formatter(errors, request) -> ProblemDetail``.

        ``errors`` is the raw list of ``{"loc", "msg", "type"}`` dicts from the
        validator. Pass ``None`` to restore the built-in RFC 9457 output.
        ``wreath.validation_errors`` ships a catalogue-backed formatter that
        translates on ``type`` and negotiates ``Accept-Language``.
        """
        self._validation_formatter = formatter

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
            for index, (before, _after, is_sync) in enumerate(global_hooks):
                if before is None:
                    continue
                try:
                    candidate = before(request) if is_sync else await before(request)
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
            static_match = self._static_matcher.match(path)
            if static_match is not None:
                matched = static_match
                if global_hooks:
                    request._set_route_outcome("static")
            else:
                # Through the exception path rather than straight to a
                # ProblemResponse, so a registered 404 status handler (or a
                # NotFound exception handler) covers a routing miss -- which is
                # the case people register one for. With nothing registered this
                # produces the same response it always did.
                response = await self._handle_exception(request, NotFound("Not Found"))
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
                    # the handler/serialize timing below is a plain local call,
                    # and propagate it so dependency seams (PostgreSQL, HTTP
                    # client) can record their phases from inside the handler.
                    # Set-without-reset is safe: the request task's context dies
                    # with it, and the protocol severs the context at completion
                    # so an escaped binding no-ops instead of writing stale.
                    flight_phase = scope._flight_phase
                    _phase_marker.set(flight_phase)
        elif "_wreath_flight" in scope:
            ids = self._flight_route_ids or self._build_flight_route_ids()
            attribution = ids.get(handler)
            if attribution is not None:
                scope["_wreath_flight"] = attribution
        if request is None:
            request = Request(scope, receive, path_params, self._limits)
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
        hooks = self._global_hooks
        # Counted down by index rather than `reversed(hooks[:active_global])`,
        # which allocated a fresh list slice on every response.
        index = active_global
        while index:
            index -= 1
            after = hooks[index][1]
            if after is not None:
                try:
                    response = _coerce_response(await after(request, response))
                except Exception as error:
                    response = await self._handle_exception(request, error)

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
        # A WebSocket route carries the same `@authenticated`/`@roles`/
        # `@permissions`/`@authorize` metadata an HTTP route does, and it used to
        # be ignored here -- the handler ran for anyone who could reach the path.
        # Enforced before the handshake, so a refused caller never gets an
        # accepted socket: an ASGI server turns a pre-accept close into its own
        # rejection response.
        identity: Identity | None = None
        requirement = requirement_for(handler)
        if (
            requirement.authenticated
            or requirement.role_checks
            or requirement.permission_checks
            or requirement.policies
        ):
            # Authentication backends read headers and cookies, which a
            # WebSocket scope carries; `method` is synthesized on a copy because
            # the handshake is a GET and a backend may look.
            request = Request({**scope, "method": "GET"}, receive, path_params, self._limits)
            try:
                await self._authorize_request(request, requirement)
            except HTTPException:
                await send({"type": "websocket.close", "code": 1008})
                return
            identity = request.identity
        websocket = WebSocket(scope, receive, send, path_params, identity=identity)
        try:
            await cast("WebSocketHandler", handler)(websocket)
        except WebSocketDisconnect:
            # The peer left; nothing further to send.
            return

    async def _handle_exception(
        self, request: Request, error: Exception
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse:
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
        binding_specs = self._application_image.binding_specs()
        router = CompiledRouter(self._routing)
        app_middleware = tuple(
            item[2] for item in sorted(self._middleware, key=lambda item: (item[0], item[1]))
        )
        global_middleware = tuple(
            item[2]
            for item in sorted(self._global_middleware, key=lambda item: (item[0], item[1]))
        )
        # (before_hook, after_hook, is_sync). A synchronous before_sync hook is
        # dispatched without a coroutine/await in the global loop; otherwise the
        # async before hook is awaited as before.
        self._global_hooks = tuple(
            (before_sync, getattr(item, "after", None), True)
            if (before_sync := getattr(item, "before_sync", None)) is not None
            else (getattr(item, "before", None), getattr(item, "after", None), False)
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
        router.compile()
        if self._ws_router is not None:
            self._ws_router.compile()
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
        on shutdown. With ``plan`` None the request-path capture seam stays a
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

        Reached only when the native context reported ``flight == 2`` (armed) and
        a capture plan is installed -- i.e. only on the Forensic-sampled subset of
        requests. Capture is gated on *active* runtime arms, narrows to each arm's
        compiled plan (within the startup ceiling), and counts one match per
        active arm. The native ``_flight_capture`` is deny-by-default and redacts
        each field, so a header no arm permits never leaves recorder memory.
        Returns the active-arm snapshot (shared with the completion and dependency
        seams) or ``None`` when nothing is armed.
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

        Called only when :meth:`_capture_request` found active arms, so the match
        is already counted; this adds the response side under the same arm
        snapshot. Response headers resolve against the same direction-agnostic
        redaction rules as the request headers. The request body is captured only
        from what the handler already buffered -- never a fresh read that would
        consume the stream or change behavior -- and only plain-``bytes``
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
        self, request: Request, requirement: AuthRequirement, backend: Any = None
    ) -> Response | StreamingResponse | FileResponse | PreparedResponse | None:
        """Run authentication and authorization before route middleware.

        ``backend`` overrides the app-wide auth backend for route-scoped
        authentication (e.g. the API-docs subsystem); it defaults to
        ``self._auth_backend`` so ordinary routes are unaffected.
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
            challenge = (
                None if auth_backend is None else auth_backend.challenge(request)
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

        * ``environments`` -- if set, the routes are registered *only* when the
          current environment (``env`` arg, else ``WREATH_ENV``, else
          ``"production"``) is listed. Otherwise nothing is registered and both
          paths 404 -- no existence leak. Forgetting to set an env therefore
          means production, which means docs OFF.
        * ``authenticated`` / ``permissions`` / ``authorize`` -- require auth on
          the two routes, enforced (401/403) by a route-scoped guard. ``authorize``
          is a decorator such as :func:`wreath.authorize`.
        * ``auth`` -- a backend that authenticates ONLY these two routes,
          independent of :meth:`configure_auth` (no global side effect). When it
          and the other auth args are all omitted, the docs are open within the
          allowed environments (environment gating is the only guard). A live
          try-it-out console inherits exactly this gate.

        Returns ``True`` when the routes were registered, ``False`` when the
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
            key = len(self._routes)
            if spec_state.get("bytes") is None or spec_state.get("key") != key:
                spec_state["bytes"] = _json_dumps(
                    generate_openapi(self, title=title, version=version)
                )
                spec_state["key"] = key
            return spec_state["bytes"]

        def body_html() -> str:
            key = len(self._routes)
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
        """Backwards-compatible alias for :meth:`enable_api_docs`.

        Serves the self-contained docs page and OpenAPI document **outside
        production**. The gate is the difference from what this used to do: the
        alias registered both routes unconditionally, so the shorter, older,
        more-copied spelling was the one that published the whole API surface
        from a production deployment. Pass ``environments=None`` to register
        everywhere, or use :meth:`enable_api_docs` for auth as well as env
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
                    if self._supervisor is not None:
                        await self._supervisor.stop()
                        self._supervisor = None
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
                    # Stop supervised work before the resources it depends on:
                    # stop fetching, drain in-flight, release leases (design 01).
                    if self._supervisor is not None:
                        await self._supervisor.stop()
                        self._supervisor = None
                    # App-scoped dependencies before the resources they were
                    # built from: an app-scoped generator may still want to
                    # talk to a database or HTTP client on the way out.
                    await self._app_scope.aclose()
                    for store in reversed(tuple(self._object_stores.values())):
                        close = getattr(store, "close", None)
                        if close is not None:
                            close()
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
    """Turn a handler's return value into a response object.

    Two deliberate tiers, not redundant checks: the exact-type ``kind is`` arms
    are the common fast path (str/dict/bytes build a response in one frame,
    skipping the ladder); the ``isinstance`` arms below the response-type check
    then catch the rarer subclasses (a str/bytes subclass, or a dict/list/scalar
    for JSON) that the exact-type tests miss.
    """
    kind = type(value)
    if kind is str:
        return coerce_text(value)
    if kind is dict:
        return coerce_json(value)
    if kind is bytes:
        return coerce_bytes(value)
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
